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
import copy
import os
from unittest import mock
from urllib.parse import quote_plus, urljoin
from urllib.request import pathname2url
from uuid import uuid4

import nexussdk
import pytest
from typing import Callable, Union, List

from kgforge.core.commons.files import load_yaml_from_file
from kgforge.core.resource import Resource
from kgforge.core.archetypes.store import Store
from kgforge.core.commons.context import Context
from kgforge.core.conversions.rdf import _merge_jsonld
from kgforge.core.wrappings.dict import wrap_dict
from kgforge.core.wrappings.paths import Filter, create_filters_from_dict
from kgforge.core.commons.sparql_query_builder import SPARQLQueryBuilder
from kgforge.specializations.models import DemoModel
from kgforge.specializations.stores.bluebrain_nexus import (
    BlueBrainNexus,
    _create_select_query,
)

# FIXME mock Nexus for unittests
# TODO To be port to the generic parameterizable test suite for stores in test_stores.py. DKE-135.
from kgforge.specializations.stores.nexus import Service
from utils import full_path_relative_to_root

MODEL = DemoModel(**{"origin": "directory",
                     "source": full_path_relative_to_root("tests/data/demo-model/")})
BUCKET = "test/kgforge"
NEXUS = "https://nexus-instance.org"
TOKEN = "token"
NEXUS_PROJECT_CONTEXT = {"base": "http://data.net", "vocab": "http://vocab.net",
                         "apiMappings": [{'namespace': 'https://neuroshapes.org/dash/', 'prefix': 'datashapes'}]}
VERSIONED_TEMPLATE = "{x.id}?rev={x._store_metadata._rev}"

FILE_RESOURCE_MAPPING = full_path_relative_to_root(
    "./tests/data/nexus-store/file-to-resource-mapping.hjson"
)


@pytest.fixture
def nested_resource():
    contributions = [Resource(title=f"contribution {i}") for i in range(3)]
    return Resource(type="Agent", name="someone", contributions=contributions)


@pytest.fixture
def nested_registered_resource(nested_resource):
    ingredients = [Resource(id=i, type="Ingredient") for i in range(3)]
    resource = Resource(
        id="a_recipe",
        type="Recipe",
        ingridients=ingredients,
        author=Resource(id="a_person", type="Person"),
    )
    do_recursive(add_metadata, resource)
    return resource


@pytest.fixture
def metadata_data_compacted():
    return {
        "_deprecated": False,
        "_updatedBy": "http://integration.kfgorge.test",
        "_rev": 1,
        "_constrainedBy":"http://schema.org/Building"
    }


@pytest.fixture
def store_metadata_value(metadata_data_compacted):
    data = {"id": "placeholder"}
    data.update(metadata_data_compacted)
    return data


@pytest.fixture
def registered_building(building, model_context, store_metadata_value):
    building.context = (
        model_context.iri
        if model_context.is_http_iri()
        else model_context.document["@context"]
    )
    if model_context.base:
        building.id = f"{model_context.base}{str(uuid4())}"
    else:
        building.id = f"{urljoin('file:', pathname2url(os.getcwd()))}/{str(uuid4())}"
    store_metadata_value["id"] = building.id
    store_metadata_value["_constrainedBy"] = "http://schema.org/Building"
    building._store_metadata = wrap_dict(store_metadata_value)
    return building


@pytest.fixture
def registered_person(person, store_metadata_value):
    custom_context = person.context
    if custom_context.base:
        person.id = f"{person.base}{str(uuid4())}"
    else:
        person.id = f"{urljoin('file:', pathname2url(os.getcwd()))}/{str(uuid4())}"
    store_metadata_value["id"] = person.id
    person._store_metadata = wrap_dict(store_metadata_value)
    return person


@pytest.fixture
def production_configuration():
    return load_yaml_from_file(
        full_path_relative_to_root("./examples/notebooks/use-cases/prod-forge-nexus.yml")
    )


@pytest.fixture
def store_config(production_configuration):
    return production_configuration["Store"]


@pytest.fixture
@mock.patch("nexussdk.projects.fetch", return_value=NEXUS_PROJECT_CONTEXT)
@mock.patch("nexussdk.resources.fetch", side_effect=nexussdk.HTTPError("404"))
def nexus_store(context_project_patch, metadata_context_patch, store_config):

    store_config_cp = copy.deepcopy(store_config)
    store_config_cp["endpoint"] = NEXUS
    store_config_cp["bucket"] = BUCKET
    store_config_cp["file_resource_mapping"] = FILE_RESOURCE_MAPPING
    store_config_cp["model"] = MODEL
    store_config_cp["token"] = TOKEN

    return BlueBrainNexus(**store_config_cp)


@pytest.fixture
def nexus_store_unauthorized():
    return BlueBrainNexus(endpoint=NEXUS, bucket=BUCKET, token="invalid token")


@pytest.fixture
def nexus_context():
    context_document = dict()
    context_document["@base"] = NEXUS_PROJECT_CONTEXT["base"]
    context_document["@vocab"] = NEXUS_PROJECT_CONTEXT["vocab"]
    for mapping in NEXUS_PROJECT_CONTEXT['apiMappings']:
        context_document[mapping['prefix']] = mapping['namespace']
    return Context(context_document)


def test_config_error():
    with pytest.raises(ValueError):
        BlueBrainNexus(model={}, endpoint="test", bucket="invalid", token="")


def test_config(nexus_store):
    assert nexus_store.organisation == "test"
    assert nexus_store.project == "kgforge"
    assert nexus_store.endpoint == NEXUS
    assert nexus_store.context.base == NEXUS_PROJECT_CONTEXT["base"]


def test_freeze_fail(nexus_store: Store, nested_resource):
    """nested resource is not registered, thus freeze will fail"""
    nexus_store.versioned_id_template = "{x.id}?rev={x._store_metadata._rev}"
    nested_resource.id = "abc"
    add_metadata(nested_resource)


def test_freeze_nested(nexus_store: Store, nested_registered_resource):
    nexus_store.versioned_id_template = "{x.id}?rev={x._store_metadata._rev}"
    nexus_store.freeze(nested_registered_resource)
    do_recursive(assert_frozen_id, nested_registered_resource)


def test_to_resource(nexus_store, registered_building, building_jsonld, store_config):
    context_path = store_config["vocabulary"]["metadata"]["iri"]
    context = _merge_jsonld(registered_building.context, context_path)
    payload = building_jsonld(registered_building, "compacted", True, None)
    payload["@context"] = context
    result = nexus_store.service.to_resource(payload)
    assert str(result) == str(registered_building)
    assert getattr(result, "context") == registered_building.context
    assert str(result._store_metadata) == str(registered_building._store_metadata)


@pytest.mark.parametrize("_constrainedBy, schema_id, expected_params, expected_url_template, expected_url_tag_template",
                         [
                            pytest.param(       
                                ("http://schema.org/Building"),
                                (None),
                                ({"rev":1}),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("http://schema.org/Building"),"{}"))),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("http://schema.org/Building"),"{}", "tags"))),
                                id="tag-constrained-no-schema",
                            ),
                            pytest.param(       
                                (None),
                                ("http://schema.org/Building"),
                                ({"rev":1}),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("http://schema.org/Building"),"{}"))),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("_"),"{}", "tags"))),
                                id="tag-no-constrained-schema",
                            ),
                            pytest.param(       
                                ("http://schema.org/Building"),
                                ("http://schema.org/AnotherBuilding"),
                                ({"rev":1}),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("http://schema.org/AnotherBuilding"),"{}"))),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("http://schema.org/Building"),"{}", "tags"))),
                                id="tag-constrainedby-schema",
                            ),
                            pytest.param(       
                                (None),
                                (None),
                                ({"rev":1}),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("_"),"{}"))),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("_"),"{}", "tags"))),
                                id="tag-no-constrainedby-no-schema",
                            ),
                            pytest.param(       
                                (Service.UNCONSTRAINED_SCHEMA),
                                (None),
                                ({"rev":1}),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("_"),"{}"))),
                                ("/".join((NEXUS,"resources",BUCKET, quote_plus("_"),"{}", "tags"))),
                                id="tag-unconstrained",
                            )
                         ])
def test_prepare_tag_uri(nexus_store, registered_building, _constrainedBy, schema_id, expected_params, expected_url_template, expected_url_tag_template):
    
    registered_building._store_metadata._constrainedBy = _constrainedBy
    url, params = nexus_store.service._prepare_uri(registered_building, schema_id)
    expected_url = expected_url_template.format(quote_plus(registered_building.id))
    
    assert params == expected_params
    assert url == expected_url

    tagValue = "aTag"
    url, data, params = nexus_store.service._prepare_tag(registered_building, tagValue)
    expected_url_tag = expected_url_tag_template.format(quote_plus(registered_building.id))
    expected_data = {"tag":tagValue, "rev":registered_building._store_metadata._rev}
   
    assert params == expected_params
    assert data == expected_data
    assert url == expected_url_tag


@pytest.mark.parametrize("url,is_file, expected",
                         [
                            pytest.param(       
                                ("myverycoolid123456789"),
                                (True),
                                ("https://nexus-instance.org/files/test/kgforge/myverycoolid123456789"),
                                id="simple-file-id",
                            ),
                            pytest.param(       
                                ("http://data.net/myverycoolid123456789"),
                                (False),
                                ("https://nexus-instance.org/resources/test/kgforge/_/http%3A%2F%2Fdata.net%2Fmyverycoolid123456789"),
                                id="simple-resource-id",
                            ),
                            pytest.param(
                                ("http://data.net/07ed2dab-587a-4144-90c7-4cdd252cfa3f"),
                                (True),
                                ("https://nexus-instance.org/files/test/kgforge/http%3A%2F%2Fdata.net%2F07ed2dab-587a-4144-90c7-4cdd252cfa3f"),
                                id="file-id",
                            ),
                            pytest.param(
                                ("https://nexus-instance.org/files/test/kgforge/myverycoolid123456789"),
                                (True),
                                ("https://nexus-instance.org/files/test/kgforge/http%3A%2F%2Fdata.net%2Fmyverycoolid123456789"),
                                id="file-self",
                            )
                            ,
                            pytest.param(
                                ("https://nexus-instance.org/resources/test/kgforge/datashapes:example/43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                (False),
                                ("https://nexus-instance.org/resources/test/kgforge/https%3A%2F%2Fneuroshapes.org%2Fdash%2Fexample/http%3A%2F%2Fdata.net%2F43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                id="resource-schema-self",
                            ),
                            pytest.param(
                                ("https://nexus-instance.org/resources/test/kgforge/_/43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                (False),
                                ("https://nexus-instance.org/resources/test/kgforge/_/http%3A%2F%2Fdata.net%2F43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                id="resource-empty-schema-self",
                            ),
                            pytest.param(
                                ("https://nexus-instance.org/files/test/kgforge/http%3A%2F%2Fdata.net%2F632a7644-b07e-4fcd-a537-9162e3444106"),
                                (True),
                                ("https://nexus-instance.org/files/test/kgforge/http%3A%2F%2Fdata.net%2F632a7644-b07e-4fcd-a537-9162e3444106"),
                                id="file-given-expanded-url-encoded-self",
                            ),
                            pytest.param(
                                ("https://nexus-instance.org/resources/test/kgforge/_/http%3A%2F%2Fdata.net%2F43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                (False),
                                ("https://nexus-instance.org/resources/test/kgforge/_/http%3A%2F%2Fdata.net%2F43edd8bf-5dfe-45cd-b6d8-1a604dd6beca"),
                                id="resource-empty-schema-url-encoded-self",
                            )
                            
                            
                         ])
def test_rewrite_uri(nexus_store, nexus_context, url, is_file, expected):
    uri = nexus_store.rewrite_uri(url, context=nexus_context, is_file=is_file, encoding=None)
    assert expected == uri


class TestQuerying:
    @pytest.fixture
    def context(self):
        document = {
            "@context": {
                "@vocab": "http://example.org/vocab/",
                "contribution": {
                    "@id": "https://neuroshapes.org/contribution",
                    "@type": "@id",
                },
                "agent": {"@id": "http://www.w3.org/ns/prov#agent", "@type": "@id"},
                "type": "rdf:type",
                "Person": "http://schema.org/Person",
                "address": "http://schema.org/address",
                "name": "http://schema.org/name",
                "postalCode": "http://schema.org/postalCode",
                "streetAddress": "http://schema.org/streetAddress",
                "deprecated": "https://bluebrain.github.io/nexus/vocabulary/deprecated",
                "identifier": {"@type": "@id", "@id": "http://schema.org/identifier"},
            }
        }
        return Context(document)

    @pytest.mark.parametrize(
        "filters,expected",
        [
            pytest.param(
                (Filter(["agent", "name"], "__eq__", "Allen Institute"),),
                (["agent/name ?v0"], ['FILTER(?v0 = "Allen Institute")']),
                id="literal",
            ),
            pytest.param(
                (Filter(["address", "postalCode"], "__lt__", 50070),),
                (["address/postalCode ?v0"], ["FILTER(?v0 < 50070)"]),
                id="number-lt",
            ),
            pytest.param(
                (Filter(["address", "postalCode"], "__gt__", 50070),),
                (["address/postalCode ?v0"], ["FILTER(?v0 > 50070)"]),
                id="number-gt",
            ),
            pytest.param(
                (Filter(["address", "postalCode"], "__ge__", 50070),),
                (["address/postalCode ?v0"], ["FILTER(?v0 >= 50070)"]),
                id="number-ge",
            ),
            pytest.param(
                (Filter(["address", "postalCode"], "__le__", 50070),),
                (["address/postalCode ?v0"], ["FILTER(?v0 <= 50070)"]),
                id="number-le",
            ),
            pytest.param(
                (Filter(["building", "height"], "__eq__", 7.0),),
                (["building/height ?v0"], ["FILTER(?v0 = 7.0)"]),
                id="float-str-parsed",
            ),
            pytest.param(
                (Filter(["building", "height"], "__eq__", "2022"),),
                (["building/height ?v0"], ['FILTER(?v0 = "2022")']),
                id="number-str-not-parsed",
            ),
            pytest.param(
                (Filter(["createdAt"], "__ge__", "2020-10-20T13:53:22.880Z^^xsd:dateTime"),),
                (["createdAt ?v0"], ['FILTER(?v0 >= "2020-10-20T13:53:22.880Z"^^xsd:dateTime)']),
                id="datetime-ge",
            ),
            pytest.param(
                (Filter(["createdAt"], "__eq__", "2020-10-20T13:53:22.880Z"),),
                (["createdAt ?v0"], ['FILTER(?v0 = "2020-10-20T13:53:22.880Z")']),
                id="datetime-str-eq-not-parsed",
            ),
            pytest.param(
                (Filter(["deprecated"], "__eq__", False),),
                (["deprecated ?v0"], ["FILTER(?v0 = 'false'^^xsd:boolean)"]),
                id="boolean-false",
            ),
            pytest.param(
                (Filter(["deprecated"], "__eq__", True),),
                (["deprecated ?v0"], ["FILTER(?v0 = 'true'^^xsd:boolean)"]),
                id="boolean-true",
            ),
            pytest.param(
                (Filter(["type"], "__eq__", "Person"),),
                (["type Person"], []),
                id="iri-eq",
            ),
            pytest.param(
                (Filter(["type"], "__ne__", "Person"),),
                (["type ?v0"], ["FILTER(?v0 != Person)"]),
                id="iri-ne",
            ),
            pytest.param(
                (
                    Filter(
                        ["type"], "__eq__", "https://github.com/BlueBrain/nexus-forge"
                    ),
                ),
                (["type <https://github.com/BlueBrain/nexus-forge>"], []),
                id="filter-by-url",
            ),
            pytest.param(
                (
                    Filter(["type"], "__ne__", "Person"),
                    Filter(["name"], "__eq__", "toto"),
                ),
                (
                    ["type ?v0", "name ?v1"],
                    ["FILTER(?v0 != Person)", 'FILTER(?v1 = "toto")'],
                ),
                id="iri-ne-name-eq",
            ),
            pytest.param(
                (
                    Filter(["type"], "__ne__", "Person"),
                    Filter(
                        operator="__eq__",
                        path=["affiliation", "id"],
                        value="https://www.grid.ac/institutes/grid.5333.6",
                    ),
                    Filter(["identifier"], "__eq__", "http://orcid.org/id"),
                ),
                (
                    [
                        "type ?v0",
                        "affiliation <https://www.grid.ac/institutes/grid.5333.6>",
                        "identifier <http://orcid.org/id>",
                    ],
                    ["FILTER(?v0 != Person)"],
                ),
                id="filter-by-id",
            ),
        ],
    )
    def test_filter_to_query_statements(self, context, filters, expected):
        statements = SPARQLQueryBuilder.build(None, None, context, list(filters))
        assert statements == expected

    @pytest.mark.parametrize(
        "filters",
        [
            pytest.param(
                (Filter(["agent", "name"], "__le__", "Allen Institute"),),
                id="range_query_str",
            )
        ]
    )
    def test_filter_to_query_statements_exceptions(self, context, filters):
        with pytest.raises(ValueError):
            SPARQLQueryBuilder.build(None, None, context, list(filters))

    def test_create_select_query(self):
        statements = f"?id type <https://github.com/BlueBrain/nexus-forge>"
        vars_ = ["?id", "?project"]
        query = _create_select_query(vars_, statements, distinct=False, search_in_graph=True)
        assert (
            query
            == "SELECT ?id ?project WHERE { Graph ?g {?id type <https://github.com/BlueBrain/nexus-forge>}}"
        )
        query = _create_select_query(vars_, statements, distinct=True, search_in_graph=True)
        assert (
            query
            == "SELECT DISTINCT ?id ?project WHERE { Graph ?g {?id type <https://github.com/BlueBrain/nexus-forge>}}"
        )
        query = _create_select_query(vars_, statements, distinct=False, search_in_graph=False)
        assert (
            query
            == "SELECT ?id ?project WHERE {?id type <https://github.com/BlueBrain/nexus-forge>}"
        )
        query = _create_select_query(vars_, statements, distinct=True, search_in_graph=False)
        assert (
            query
            == "SELECT DISTINCT ?id ?project WHERE {?id type <https://github.com/BlueBrain/nexus-forge>}"
        )

    @pytest.mark.parametrize(
        "filters,expected",
        [
            pytest.param(
                ({"type": "Person"}),
                ([Filter(operator="__eq__", path=["type"], value="Person")]),
                id="json_filter_type",
            ),
            pytest.param(
                (
                    {
                        "type": "Contribution",
                        "agent": {
                            "name": "John Doe",
                            "affiliation": {"type": "Organization", "name": "EPFL"},
                        },
                        "hadRole": {"label": "PI"},
                        "description": "A description",
                    }
                ),
                (
                    [
                        Filter(operator="__eq__", path=["type"], value="Contribution"),
                        Filter(
                            operator="__eq__", path=["agent", "name"], value="John Doe"
                        ),
                        Filter(
                            operator="__eq__",
                            path=["agent", "affiliation", "type"],
                            value="Organization",
                        ),
                        Filter(
                            operator="__eq__",
                            path=["agent", "affiliation", "name"],
                            value="EPFL",
                        ),
                        Filter(
                            operator="__eq__", path=["hadRole", "label"], value="PI"
                        ),
                        Filter(
                            operator="__eq__",
                            path=["description"],
                            value="A description",
                        ),
                    ]
                ),
                id="nested_json_filter_type",
            ),
            pytest.param(
                (
                    {
                        "type": "Person",
                        "affiliation": {
                            "type": "Organization",
                            "id": "https://www.grid.ac/institutes/grid.5333.6",
                        },
                    }
                ),
                (
                    [
                        Filter(operator="__eq__", path=["type"], value="Person"),
                        Filter(
                            operator="__eq__",
                            path=["affiliation", "type"],
                            value="Organization",
                        ),
                        Filter(
                            operator="__eq__",
                            path=["affiliation", "id"],
                            value="https://www.grid.ac/institutes/grid.5333.6",
                        ),
                    ]
                ),
                id="json_filter_id",
            ),
            pytest.param(
                (
                    {
                        "type": "Person",
                        "affiliation/id":"https://www.grid.ac/institutes/grid.5333.6"
                    }
                ),
                (
                    [
                        Filter(operator="__eq__", path=["type"], value="Person"),
                        Filter(
                            operator="__eq__",
                            path=["affiliation", "id"],
                            value="https://www.grid.ac/institutes/grid.5333.6",
                        )
                    ]
                ),
                id="json_key_sequence_path",
            )
        ],
    )
    def test_dict_to_filters(self, filters, expected):
        filters_from_dict = create_filters_from_dict(filters)
        assert filters_from_dict == expected


# Helpers


def assert_frozen_id(resource: Resource):
    assert resource.id.endswith("?rev=" + str(resource._store_metadata["_rev"]))


def add_metadata(resource: Resource):
    metadata = {
        "_self": resource.id,
        "_constrainedBy": "https://bluebrain.github.io/nexus/schemas/unconstrained.json",
        "_project": "https://nexus/org/prj",
        "_rev": 1,
        "_deprecated": False,
        "_createdAt": "2019-03-28T13:40:38.934Z",
        "_createdBy": "https://nexus/u1",
        "_updatedAt": "2019-03-28T13:40:38.934Z",
        "_updatedBy": "https://nexus/u1",
        "_incoming": "https:/nexus/incoming",
        "_outgoing": "https://nexux/outgoing",
    }
    resource._synchronized = True
    resource._validated = True
    resource._store_metadata = wrap_dict(metadata)


def do_recursive(fun: Callable, data: Union[Resource, List[Resource]], *args) -> None:
    if isinstance(data, List) and all(isinstance(x, Resource) for x in data):
        for x in data:
            fun(x, *args)
    elif isinstance(data, Resource):
        fun(data, *args)
        for _, v in data.__dict__.items():
            if isinstance(v, (Resource, List)):
                do_recursive(fun, v, *args)
    else:
        raise TypeError("not a Resource nor a list of Resource")
