#
# Blue Brain Nexus Forge is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Blue Brain Nexus Forge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser
# General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Blue Brain Nexus Forge. If not, see <https://choosealicense.com/licenses/lgpl-3.0/>.

import asyncio
import copy

import json
import mimetypes
import re
from datetime import datetime
from asyncio import Semaphore, Task
from enum import Enum

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Type
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

from requests import HTTPError
import nexussdk as nexus
import requests
from aiohttp import ClientSession, MultipartWriter
from aiohttp.hdrs import CONTENT_DISPOSITION, CONTENT_TYPE

from kgforge.core.commons.dictionaries import update_dict
from kgforge.core.commons.es_query_builder import ESQueryBuilder
from kgforge.core.commons.sparql_query_builder import SPARQLQueryBuilder
from kgforge.core.resource import Resource
from kgforge.core.archetypes.model import Model
from kgforge.core.archetypes.store import Store
from kgforge.core.archetypes.mapping import Mapping
from kgforge.core.archetypes.mapper import Mapper
from kgforge.core.archetypes.resolver import Resolver
from kgforge.core.commons.actions import LazyAction
from kgforge.core.commons.context import Context
from kgforge.core.commons.exceptions import (
    DeprecationError,
    DownloadingError,
    QueryingError,
    RegistrationError,
    RetrievalError,
    TaggingError,
    UpdatingError,
    UploadingError,
)
from kgforge.core.commons.execution import run, not_supported, catch_http_error
from kgforge.core.commons.files import is_valid_url
from kgforge.core.conversions.json import as_json
from kgforge.core.conversions.rdf import as_jsonld
from kgforge.core.wrappings.dict import DictWrapper
from kgforge.core.wrappings.paths import Filter, create_filters_from_dict
from kgforge.specializations.mappers.dictionaries import DictionaryMapper
from kgforge.specializations.mappings.dictionaries import DictionaryMapping
from kgforge.specializations.stores.nexus.service import BatchAction, Service, _error_message


class CategoryDataType(Enum):
    DATETIME = "datetime"
    NUMBER = "number"
    BOOLEAN = "boolean"
    LITERAL = "literal"


type_map = {
    datetime: CategoryDataType.DATETIME,
    str: CategoryDataType.LITERAL,
    bool: CategoryDataType.BOOLEAN,
    int: CategoryDataType.NUMBER,
    float: CategoryDataType.NUMBER,
    complex: CategoryDataType.NUMBER,
}

format_type = {
    CategoryDataType.DATETIME: lambda x: f'"{x}"^^xsd:dateTime',
    CategoryDataType.NUMBER: lambda x: x,
    CategoryDataType.LITERAL: lambda x: f'"{x}"',
    CategoryDataType.BOOLEAN: lambda x: "'true'^^xsd:boolean" if x else "'false'^^xsd:boolean",
}

sparql_operator_map = {
    "__lt__": "<",
    "__le__": "<=",
    "__eq__": "=",
    "__ne__": "!=",
    "__gt__": ">",
    "__ge__": ">=",
}

elasticsearch_operator_map = {
    "__lt__": "lt",
    "__le__": "lte",
    "__gt__": "gt",
    "__ge__": "gte",
}


def catch_http_error_nexus(
        r: requests.Response, e: Type[BaseException], error_message_formatter=_error_message
):
    return catch_http_error(r, e, error_message_formatter)


class BlueBrainNexus(Store):
    def __init__(
            self,
            model: Optional[Model] = None,
            endpoint: Optional[str] = None,
            bucket: Optional[str] = None,
            token: Optional[str] = None,
            versioned_id_template: Optional[str] = None,
            file_resource_mapping: Optional[str] = None,
            searchendpoints: Optional[Dict] = None,
            **store_config,
    ) -> None:
        super().__init__(
            model,
            endpoint,
            bucket,
            token,
            versioned_id_template,
            file_resource_mapping,
            searchendpoints,
            **store_config,
        )

    @property
    def context(self) -> Optional[Context]:
        return self.service.context

    @property
    def metadata_context(self) -> Optional[Context]:
        return self.service.metadata_context

    @property
    def mapping(self) -> Type[Mapping]:
        return DictionaryMapping

    @property
    def mapper(self) -> Type[Mapper]:
        return DictionaryMapper

    def register(
            self, data: Union[Resource, List[Resource]], schema_id: str = None
    ) -> None:
        run(
            self._register_one,
            self._register_many,
            data,
            required_synchronized=False,
            execute_actions=True,
            exception=RegistrationError,
            monitored_status="_synchronized",
            schema_id=schema_id,
        )

    def _register_many(self, resources: List[Resource], schema_id: str) -> None:
        def register_callback(task: Task):
            result = task.result()
            if isinstance(result.response, Exception):
                self.service.synchronize_resource(
                    result.resource,
                    result.response,
                    self._register_many.__name__,
                    False,
                    False,
                )
            else:
                result.resource.id = result.response["@id"]
                if not hasattr(result.resource, "context"):
                    context = self.model_context() or self.context
                    result.resource.context = (
                        context.iri
                        if context.is_http_iri()
                        else context.document["@context"]
                    )
                self.service.synchronize_resource(
                    result.resource,
                    result.response,
                    self._register_many.__name__,
                    True,
                    True,
                )

        verified = self.service.verify(
            resources,
            self._register_many.__name__,
            RegistrationError,
            id_required=False,
            required_synchronized=False,
            execute_actions=True,
        )
        params_register = copy.deepcopy(self.service.params.get("register", {}))
        self.service.batch_request(
            verified,
            BatchAction.CREATE,
            register_callback,
            RegistrationError,
            schema_id=schema_id,
            params=params_register,
        )

    def _register_one(self, resource: Resource, schema_id: str) -> None:
        context = self.model_context() or self.context
        data = as_jsonld(
            resource,
            "compacted",
            False,
            model_context=context,
            metadata_context=None,
            context_resolver=self.service.resolve_context
        )

        schema = quote_plus(schema_id) if schema_id else "_"
        url_base = f"{self.service.url_resources}/{schema}"
        params_register = copy.deepcopy(self.service.params.get("register", None))
        identifier = resource.get_identifier()
        if identifier:
            url = f"{url_base}/{quote_plus(identifier)}"
            response = requests.put(
                url,
                headers=self.service.headers,
                data=json.dumps(data, ensure_ascii=True),
                params=params_register,
            )
        else:
            url = url_base
            response = requests.post(
                url,
                headers=self.service.headers,
                data=json.dumps(data, ensure_ascii=True),
                params=params_register,
            )
        catch_http_error_nexus(response, RegistrationError)

        response_json = response.json()
        resource.id = response_json["@id"]
        # If resource had no context, update it with the one provided by the store.
        if not hasattr(resource, "context"):
            resource.context = data["@context"]
        self.service.sync_metadata(resource, response_json)

    def _upload_many(self, paths: List[Path], content_type: str) -> List[Dict]:
        async def _bulk():
            loop = asyncio.get_event_loop()
            semaphore = Semaphore(self.service.max_connection)
            async with ClientSession(headers=self.service.headers_upload) as session:
                tasks = (_create_task(x, loop, semaphore, session) for x in paths)
                return await asyncio.gather(*tasks)

        def _create_task(path, loop, semaphore, session):
            default = "application/octet-stream"
            mime_type = content_type or mimetypes.guess_type(str(path))[0] or default
            # FIXME Nexus seems to not parse the Content-Disposition 'filename*' field  properly.
            # data = FormData()
            # data.add_field("file", path.open("rb"), content_type=mime_type, filename=path.name)
            # FIXME This hack is to prevent sending Content-Disposition with the 'filename*' field.
            data = MultipartWriter("form-data")
            part = data.append(path.open("rb"))
            part.headers[CONTENT_TYPE] = mime_type
            part.headers[
                CONTENT_DISPOSITION
            ] = f'form-data; name="file"; filename="{path.name}"'
            return loop.create_task(_upload(data, semaphore, session))

        async def _upload(data, semaphore, session):
            async with semaphore:
                async with session.post(self.service.url_files, data=data) as response:
                    body = await response.json()
                    if response.status < 400:
                        return body

                    msg = " ".join(
                        re.findall("[A-Z][^A-Z]*", body["@type"])
                    ).lower()
                    raise UploadingError(msg)

        return asyncio.run(_bulk())

    def _upload_one(self, path: Path, content_type: str) -> Dict:
        file = str(path.absolute())
        mime_type = content_type or mimetypes.guess_type(file, True)[0]
        if mime_type is None:
            mime_type = "application/octet-stream"
        try:
            response = nexus.files.create(
                self.organisation, self.project, file, content_type=mime_type
            )
        except HTTPError as e:
            raise UploadingError(_error_message(e)) from e

        return response

    # C[R]UD.

    def retrieve(
            self, id_: str, version: Optional[Union[int, str]], cross_bucket: bool, **params
    ) -> Optional[Resource]:
        """
        Retrieve a resource by its identifier from the configured store and possibly at a given version.

        :param id_: the resource identifier to retrieve
        :param version: a version of the resource to retrieve
        :param cross_bucket: instructs the configured store to whether search beyond the configured bucket (True) or not (False)
        :param params: a dictionary of parameters. Supported parameters are:
              [retrieve_source] whether to retrieve the resource payload as registered in the last update
              (default: True)
        :return: Resource
        """
        version_params = None
        if version is not None:
            if isinstance(version, int):
                version_params = {"rev": version}
            elif isinstance(version, str):
                version_params = {"tag": version}
            else:
                raise RetrievalError("incorrect 'version'")
        parsed_id = urlparse(id_)
        fragment = None
        query_params = None
        # urlparse is not separating fragment and query params when the latter are put after a fragment
        if parsed_id.fragment is not None and "?" in str(parsed_id.fragment):
            fragment_parts = urlparse(parsed_id.fragment)
            query_params = parse_qs(fragment_parts.query)
            fragment = fragment_parts.path
        elif parsed_id.fragment is not None and parsed_id.fragment != "":
            fragment = parsed_id.fragment
        elif parsed_id.query is not None and parsed_id.query != "":
            query_params = parse_qs(parsed_id.query)

        if version_params is not None:
            if not isinstance(query_params, dict):
                query_params = {}
            query_params.update(version_params)

        id_without_query = f"{parsed_id.scheme}://{parsed_id.netloc}{parsed_id.path}{'#' + fragment if fragment is not None else ''}"
        url_base = (
            self.service.url_resolver if cross_bucket else self.service.url_resources
        )
        url_resource = "/".join((url_base, "_", quote_plus(id_without_query)))
        retrieve_source = params.get('retrieve_source', True)

        if retrieve_source and not cross_bucket:
            url_source = "/".join((url_resource, "source"))
            url = url_source
        else:
            url = url_resource
        try:
            response = requests.get(url, params=query_params, headers=self.service.headers)
            catch_http_error_nexus(response, RetrievalError)
        except RetrievalError as er:

            nexus_path = f"{self.service.endpoint}/resources/" if cross_bucket else self.service.url_resources

            # Try to use the id as it was given
            if id_.startswith(nexus_path):
                url_resource = id_without_query

                url = "/".join((id_without_query, "source")) \
                    if retrieve_source and not cross_bucket \
                    else id_without_query

                response = requests.get(
                    url, params=query_params, headers=self.service.headers
                )
                catch_http_error_nexus(response, RetrievalError)
            else:
                raise er
        # finally:
        if retrieve_source and not cross_bucket:

            response_metadata = requests.get(
                url_resource, params=query_params, headers=self.service.headers
            )
            catch_http_error_nexus(response_metadata, RetrievalError)

        elif retrieve_source and cross_bucket and response and ('_self' in response.json()):

            response_metadata = requests.get(
                "/".join([response.json()["_self"], "source"]), params=query_params,
                headers=self.service.headers
            )
            catch_http_error_nexus(response, RetrievalError)

        else:
            response_metadata = True  # when retrieve_source is False

        if response and response_metadata:
            try:
                data = response.json()
                resource = self.service.to_resource(data)
            except Exception as e:
                raise ValueError(e) from e

            try:
                if retrieve_source and not cross_bucket:
                    data = response_metadata.json()
                if retrieve_source and cross_bucket:
                    resource = self.service.to_resource(response_metadata.json())
            except Exception as e:
                self.service.synchronize_resource(
                    resource, data, self.retrieve.__name__, False, False
                )
                raise ValueError(e) from e

            finally:
                self.service.synchronize_resource(
                    resource, data, self.retrieve.__name__, True, True
                )
            return resource

    def _retrieve_filename(self, id_: str) -> Tuple[str, str]:
        response = requests.get(id_, headers=self.service.headers)
        catch_http_error_nexus(response, DownloadingError)
        metadata = response.json()
        return metadata["_filename"], metadata["_mediaType"]

    def _download_many(
            self,
            urls: List[str],
            paths: List[str],
            store_metadata: Optional[DictWrapper],
            cross_bucket: bool,
            content_type: str,
            buckets: List[str]
    ) -> None:
        async def _bulk():
            loop = asyncio.get_event_loop()
            semaphore = Semaphore(self.service.max_connection)
            headers = self.service.headers_download if not content_type else update_dict(
                self.service.headers_download, {"Accept": content_type})
            async with ClientSession(headers=headers) as session:
                tasks = (
                    _create_task(x, y, z, b, loop, semaphore, session)
                    for x, y, z, b in zip(urls, paths, store_metadata, buckets)
                )
                return await asyncio.gather(*tasks)

        def _create_task(url, path, store_metadata, bucket, loop, semaphore, session):
            return loop.create_task(
                _download(url, path, store_metadata, bucket, semaphore, session)
            )

        async def _download(url, path, store_metadata, bucket, semaphore, session):
            async with semaphore:
                params_download = copy.deepcopy(self.service.params.get("download", {}))
                async with session.get(url, params=params_download) as response:

                    catch_http_error_nexus(
                        response, DownloadingError,
                        error_message_formatter=lambda e:
                        f"Downloading url {url} from bucket {bucket} failed: {_error_message(e)}"
                    )
                    with open(path, "wb") as f:
                        data = await response.read()
                        f.write(data)

        return asyncio.run(_bulk())

    def _download_one(
            self,
            url: str,
            path: str,
            store_metadata: Optional[DictWrapper],
            cross_bucket: bool,
            content_type: str,
            bucket: str
    ) -> None:

        params_download = copy.deepcopy(self.service.params.get("download", {}))
        headers = self.service.headers_download if not content_type else update_dict(
            self.service.headers_download, {"Accept": content_type})

        response = requests.get(
            url=url,
            headers=headers,
            params=params_download
        )
        catch_http_error_nexus(
            response, DownloadingError,
            error_message_formatter=lambda e: f"Downloading from bucket {bucket} failed: "
                                              f"{_error_message(e)}"
        )

        with open(path, "wb") as f:
            for chunk in response.iter_content(chunk_size=4096):
                f.write(chunk)

    def _prepare_download_one(
            self,
            url: str,
            store_metadata: Optional[DictWrapper],
            cross_bucket: bool
    ) -> Tuple[str, str]:
        if cross_bucket:
            if store_metadata is not None:
                project = store_metadata._project.split("/")[-1]
                org = store_metadata._project.split("/")[-2]
            else:
                raise ValueError(
                    f"Downloading non registered file is not allowed when cross_bucket is set to {cross_bucket}"
                )
        else:
            org = self.service.organisation
            project = self.service.project
        file_id = url.split("/")[-1]
        file_id = unquote(file_id)
        if len(file_id) < 1:
            raise DownloadingError(f"Invalid file url: {url}")
        if file_id.startswith("http"):
            url_base = url
        else:
            # this is a hack since _self and _id have the same uuid
            url_base = "/".join(
                (
                    self.service.url_base_files,
                    quote_plus(org),
                    quote_plus(project),
                    quote_plus(file_id),
                )
            )
        return url_base, f"{org}/{project}"

    # CR[U]D.

    def update(self, data: Union[Resource, List[Resource]], schema_id: str) -> None:
        run(
            self._update_one,
            self._update_many,
            data,
            id_required=True,
            required_synchronized=False,
            execute_actions=True,
            exception=UpdatingError,
            monitored_status="_synchronized",
            schema_id=schema_id,
        )

    def _update_many(self, resources: List[Resource], schema_id: str) -> None:
        update_callback = self.service.default_callback(self._update_many.__name__)
        verified = self.service.verify(
            resources,
            self._update_many.__name__,
            UpdatingError,
            id_required=True,
            required_synchronized=False,
            execute_actions=True,
        )
        params_update = copy.deepcopy(self.service.params.get("update", {}))
        self.service.batch_request(
            verified,
            BatchAction.UPDATE,
            update_callback,
            UpdatingError,
            params=params_update,
        )

    def _update_one(self, resource: Resource, schema_id: str) -> None:
        context = self.model_context() or self.context
        data = as_jsonld(
            resource,
            "compacted",
            False,
            model_context=context,
            metadata_context=None,
            context_resolver=self.service.resolve_context
        )
        url, params = self.service._prepare_uri(resource, schema_id)
        params_update = copy.deepcopy(self.service.params.get("update", {}))
        params_update.update(params)

        response = requests.put(
            url,
            headers=self.service.headers,
            data=json.dumps(data, ensure_ascii=True),
            params=params_update,
        )

        catch_http_error_nexus(response, UpdatingError)
        self.service.sync_metadata(resource, response.json())

    def tag(self, data: Union[Resource, List[Resource]], value: str) -> None:
        run(
            self._tag_one,
            self._tag_many,
            data,
            id_required=True,
            required_synchronized=True,
            exception=TaggingError,
            value=value,
        )

    def _tag_many(self, resources: List[Resource], value: str) -> None:
        tag_callback = self.service.default_callback(self._tag_many.__name__)
        verified = self.service.verify(
            resources,
            self._tag_many.__name__,
            TaggingError,
            id_required=True,
            required_synchronized=True,
            execute_actions=False,
        )
        params_tag = copy.deepcopy(self.service.params.get("tag", {}))
        self.service.batch_request(
            verified,
            BatchAction.TAG,
            tag_callback,
            TaggingError,
            tag=value,
            params=params_tag,
        )

    def _tag_one(self, resource: Resource, value: str) -> None:
        url, data, rev_param = self.service._prepare_tag(resource, value)
        params_tag = copy.deepcopy(self.service.params.get("tag", {}))
        params_tag.update(rev_param)
        response = requests.post(
            url,
            headers=self.service.headers,
            data=json.dumps(data, ensure_ascii=True),
            params=params_tag,
        )
        catch_http_error_nexus(response, TaggingError)

        self.service.sync_metadata(resource, response.json())

    # CRU[D].

    def deprecate(self, data: Union[Resource, List[Resource]]) -> None:
        run(
            self._deprecate_one,
            self._deprecate_many,
            data,
            id_required=True,
            required_synchronized=True,
            exception=DeprecationError,
            monitored_status="_synchronized",
        )

    def _deprecate_many(self, resources: List[Resource]) -> None:
        deprecate_callback = self.service.default_callback(
            self._deprecate_many.__name__
        )
        verified = self.service.verify(
            resources,
            self._deprecate_many.__name__,
            DeprecationError,
            id_required=True,
            required_synchronized=True,
            execute_actions=False,
        )
        params_deprecate = copy.deepcopy(self.service.params.get("deprecate", {}))
        self.service.batch_request(
            verified,
            BatchAction.DEPRECATE,
            deprecate_callback,
            DeprecationError,
            params=params_deprecate,
        )

    def _deprecate_one(self, resource: Resource) -> None:

        url, params = self.service._prepare_uri(resource)
        params_deprecate = copy.deepcopy(self.service.params.get("deprecate", None))

        if params_deprecate is not None:
            params_deprecate.update(params)
        else:
            params_deprecate = params

        response = requests.delete(
            url, headers=self.service.headers, params=params_deprecate
        )
        catch_http_error_nexus(response, DeprecationError)
        self.service.sync_metadata(resource, response.json())

        # Querying.

    def search(
            self, filters: List[Union[Dict, Filter]], resolvers: Optional[List[Resolver]],
            **params
    ) -> List[Resource]:

        if self.model_context() is None:
            raise ValueError("context model missing")

        debug = params.get("debug", False)
        limit = params.get("limit", 100)
        offset = params.get("offset", None)
        deprecated = params.get("deprecated", False)
        cross_bucket = params.get("cross_bucket", False)
        bucket = params.get("bucket", None)
        search_in_graph = params.get("search_in_graph", True)
        distinct = params.get("distinct", False)
        includes = params.get("includes", None)
        excludes = params.get("excludes", None)
        search_endpoint = params.get(
            "search_endpoint", self.service.sparql_endpoint["type"]
        )
        if search_endpoint not in [
            self.service.sparql_endpoint["type"],
            self.service.elastic_endpoint["type"],
        ]:
            raise ValueError(
                f"The provided search_endpoint value '{search_endpoint}' is not supported. Supported "
                f"search_endpoint values are: '{self.service.sparql_endpoint['type'], self.service.elastic_endpoint['type']}'"
            )
        if "filters" in params:
            raise ValueError(
                "A 'filters' key was provided as params. Filters should be provided as iterable."
            )

        if bucket and not cross_bucket:
            raise not_supported(("bucket", True))

        if filters:
            if isinstance(filters, list) and len(filters) > 0:
                if filters[0] is None:
                    raise ValueError("Filters cannot be None")
                elif isinstance(filters[0], dict):
                    filters = create_filters_from_dict(filters[0])
            else:
                filters = list(filters)

        if search_endpoint == self.service.sparql_endpoint["type"]:
            if includes or excludes:
                raise ValueError(
                    "Field inclusion and exclusion are not supported when using SPARQL"
                )
            project_filter = ""
            if bucket:
                project_filter = f"Filter (?_project = <{'/'.join([self.endpoint, 'projects', bucket])}>)"
            elif not cross_bucket:
                project_filter = f"Filter (?_project = <{'/'.join([self.endpoint, 'projects', self.organisation, self.project])}>)"

            query_statements, query_filters = SPARQLQueryBuilder.build(
                schema=None, resolvers=resolvers, context=self.model_context(), filters=filters
            )
            retrieve_source = params.get("retrieve_source", True)
            store_metadata_statements = []
            if retrieve_source:
                _vars = ["?id"]
                for i, k in enumerate(self.service.store_metadata_keys):
                    _vars.append(f"?{k}")
                    store_metadata_statements.insert(
                        i + 2, f"<{self.metadata_context.terms[k].id}> ?{k}")
                deprecated_filter = f"Filter (?_deprecated = {format_type[CategoryDataType.BOOLEAN](deprecated)})"
                query_filters.append(deprecated_filter)
            else:
                _vars = ["?id", "?_project", "?_rev"]
                store_metadata_statements.append(f"<{self.service.revision_property}> ?_rev")
                store_metadata_statements.append(f"<{self.service.project_property}> ?_project")
                query_statements.append(
                    f"<{self.service.deprecated_property}> {format_type[CategoryDataType.BOOLEAN](deprecated)}",
                )
            query_statements.extend(store_metadata_statements)
            statements = ";\n ".join(query_statements)
            _filters = "\n".join(
                (".\n ".join(query_filters), project_filter)
            )
            query = _create_select_query(
                _vars, f"?id {statements} . \n {_filters}", distinct,
                search_in_graph
            )
            # support @id and @type
            resources = self.sparql(query, debug=debug, limit=limit, offset=offset)
            params_retrieve = copy.deepcopy(self.service.params.get("retrieve", {}))
            params_retrieve['retrieve_source'] = retrieve_source
            results = self.service.batch_request(
                resources, BatchAction.FETCH, None, QueryingError, params=params_retrieve
            )
            resources = []
            for result in results:
                resource = result.resource
                if retrieve_source:
                    store_metadata_response = as_json(
                        result.resource, expanded=False,
                        store_metadata=False,
                        model_context=None,
                        metadata_context=None,
                        context_resolver=None
                    )  # store_metadata is obtained from SPARQL (resource) and
                    # not from server (response) because of retrieve_source==True
                else:
                    store_metadata_response = result.response  # dict
                try:
                    resource = self.service.to_resource(result.response)
                except Exception as e:
                    self.service.synchronize_resource(
                        resource, store_metadata_response, self.search.__name__, False, False
                    )
                    raise ValueError(e) from e
                finally:
                    self.service.synchronize_resource(
                        resource, store_metadata_response, self.search.__name__, True, False
                    )
                resources.append(resource)
            return resources
        else:
            if isinstance(self.service.elastic_endpoint["view"], LazyAction):
                self.service.elastic_endpoint["view"] = self.service.elastic_endpoint[
                    "view"
                ].execute()

            elastic_mapping = (
                self.service.elastic_endpoint["view"]["mapping"]
                if "mapping" in self.service.elastic_endpoint["view"]
                else None
            )
            default_str_keyword_field = self.service.elastic_endpoint[
                "default_str_keyword_field"
            ]
            deprecated_property_context_term = self.service.metadata_context.find_term(
                self.service.deprecated_property
            )
            project_property_context_term = self.service.metadata_context.find_term(
                self.service.project_property
            )
            filters.append(
                Filter(
                    operator="__eq__",
                    path=[
                        deprecated_property_context_term.name
                        if deprecated_property_context_term is not None
                        else "_deprecated"
                    ],
                    value=deprecated,
                )

            )
            _project = None
            if bucket:
                _project = '/'.join([self.endpoint, 'projects', bucket])

            elif not cross_bucket:
                _project = '/'.join([self.endpoint, 'projects', self.organisation, self.project])

            if _project:
                filters.append(
                    Filter(
                        operator="__eq__",
                        path=[
                            project_property_context_term.name
                            if project_property_context_term is not None
                            else "_project"
                        ],
                        value=_project
                    )
                )

            query = ESQueryBuilder.build(
                elastic_mapping,
                resolvers,
                self.model_context(),
                filters,
                default_str_keyword_field=default_str_keyword_field,
                includes=includes,
                excludes=excludes,
            )

            return self.elastic(
                json.dumps(query), debug=debug, limit=limit, offset=offset
            )

    @staticmethod  # for testing
    def reformat_contexts(model_context: Context, metadata_context: Optional[Context]):
        ctx = {}

        if metadata_context and metadata_context.document:
            ctx.update(BlueBrainNexus._context_to_dict(metadata_context))

        ctx.update(BlueBrainNexus._context_to_dict(model_context))

        prefixes = model_context.prefixes

        return ctx, prefixes, model_context.vocab

    def get_context_prefix_vocab(self) -> Tuple[Optional[Dict], Optional[Dict], Optional[str]]:
        return BlueBrainNexus.reformat_contexts(self.model_context(), self.service.metadata_context)

    def _sparql(self, query: str) -> List[Resource]:

        response = requests.post(
            self.service.sparql_endpoint["endpoint"],
            data=query,
            headers=self.service.headers_sparql,
        )
        catch_http_error_nexus(response, QueryingError)

        data = response.json()

        context = self.model_context() or self.context
        return SPARQLQueryBuilder.build_resource_from_response(query, data, context)

    def _elastic(self, query: str) -> List[Resource]:

        response = requests.post(
            self.service.elastic_endpoint["endpoint"],
            data=query,
            headers=self.service.headers_elastic,
        )
        catch_http_error_nexus(response, QueryingError)

        results = response.json()
        return [
            self.service.to_resource(
                hit["_source"],
                True,
                **{
                    "id": hit.get("_id", None),
                    "_index": hit.get("_index", None),
                    "_score": hit.get("_score", None),
                },
            )
            for hit in results["hits"]["hits"]
        ]

    # Utils.

    def _initialize_service(
            self,
            endpoint: Optional[str],
            bucket: Optional[str],
            token: Optional[str],
            searchendpoints: Optional[Dict] = None,
            **store_config,
    ) -> Any:
        try:
            self.organisation, self.project = self.bucket.split("/")
            max_connection = store_config.pop("max_connection", 50)
            if max_connection <= 0:
                raise ValueError(
                    f"max_connection value should be great than 0 but {max_connection} is provided"
                )
            store_context_config = store_config.pop("vocabulary", {})
            nexus_metadata_context = store_context_config.get(
                "metadata",
                {
                    "iri": Service.NEXUS_CONTEXT_FALLBACK,
                    "local_iri": Service.NEXUS_CONTEXT_FALLBACK,
                },
            )
            nexus_context_iri = nexus_metadata_context.get("iri")
            nexus_context_local_iri = nexus_metadata_context.get("local_iri")
            namespace = store_context_config.get(
                "namespace", Service.NEXUS_NAMESPACE_FALLBACK
            )
            project_property = store_context_config.get(
                "project_property", Service.PROJECT_PROPERTY_FALLBACK
            )
            deprecated_property = store_context_config.get(
                "deprecated_property", Service.DEPRECATED_PROPERTY_FALLBACK
            )
            content_type = store_config.pop("Content-Type", "application/ld+json")
            accept = store_config.pop("Accept", "application/ld+json")
            files_upload_config = store_config.pop(
                "files_upload", {"Accept": "application/ld+json"}
            )
            files_download_config = store_config.pop(
                "files_download", {"Accept": "*/*"}
            )
            params = store_config.pop("params", {})
        except Exception as ve:
            raise ValueError(f"Store configuration error: {ve}") from ve

        return Service(
            endpoint=endpoint,
            org=self.organisation,
            prj=self.project,
            token=token,
            model_context=self.model_context(),
            max_connection=max_connection,
            searchendpoints=searchendpoints,
            store_context=nexus_context_iri,
            store_local_context=nexus_context_local_iri,
            namespace=namespace,
            project_property=project_property,
            deprecated_property=deprecated_property,
            content_type=content_type,
            accept=accept,
            files_upload_config=files_upload_config,
            files_download_config=files_download_config,
            **params,
        )

    def rewrite_uri(self, uri: str, context: Context, **kwargs) -> str:
        is_file = kwargs.get("is_file", True)
        encoding = kwargs.get("encoding", None)

        # try decoding the url first
        raw_url = unquote(uri)
        if is_file:  # for files
            url_base = '/'.join([self.endpoint, 'files', self.bucket])
        else:  # for resources
            url_base = '/'.join([self.endpoint, 'resources', self.bucket])
        matches = re.match(r"[\w\.:%/-]+/(\w+):(\w+)/[\w\.-/:%]+", raw_url)
        if matches:
            groups = matches.groups()
            old_schema = f"{groups[0]}:{groups[1]}"
            resolved = context.expand(groups[0])
            if raw_url.startswith(url_base):
                extended_schema = resolved + groups[1]
                url = raw_url.replace(old_schema, quote_plus(extended_schema))
                schema_and_id = url.split(url_base + "/")[1]
                id_ = schema_and_id.split(quote_plus(extended_schema) + "/")[-1]
                if not is_valid_url(id_):
                    resolved_id = context.resolve_iri(id_)
                else:
                    resolved_id = id_
                return url.replace(id_, quote_plus(resolved_id))

            extended_schema = ''.join([resolved, groups[1]])
            url = raw_url.replace(old_schema, extended_schema)
        else:
            url = raw_url
        if url.startswith(url_base):
            schema_and_id = url.split(url_base)[1]
            if "/_/" in schema_and_id:  # has _ schema
                id_ = schema_and_id.split("/_/")[-1]
            else:
                id_ = schema_and_id.split("/")[-1]
            if not is_valid_url(id_):
                resolved_id = context.resolve_iri(id_)
            else:
                resolved_id = id_
            if resolved_id in schema_and_id:
                return uri  # expanded already given

            return url.replace(id_, quote_plus(resolved_id))
        if not is_file and "/_/" not in url:  # adding _ for empty schema
            uri = "/".join((url_base, "_", quote_plus(url, encoding=encoding)))
        else:
            uri = "/".join((url_base, quote_plus(url, encoding=encoding)))
        return uri

    def _freeze_many(self, resources: List[Resource]) -> None:
        raise not_supported()


def _create_select_query(vars_, statements, distinct, search_in_graph):
    where_clauses = (
        f"{{ Graph ?g {{{statements}}}}}" if search_in_graph else f"{{{statements}}}"
    )
    join_vars_ = ' '.join(vars_)
    select_vars = f"DISTINCT {join_vars_}" if distinct else f"{join_vars_}"
    return f"SELECT {select_vars} WHERE {where_clauses}"
