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

from typing import Callable, Dict, List, Optional, Union, Any

import itertools

import requests

from kgforge.core.archetypes.store import Store
from kgforge.core.conversions.json import as_json
from kgforge.core.resource import encode
from kgforge.core.wrappings import Filter, FilterOperator
from kgforge.specializations.mappers.dictionaries import DictionaryMapper
from kgforge.specializations.mappings.dictionaries import DictionaryMapping
from kgforge.specializations.resolvers.entity_linking.service.entity_linking_service import (
    EntityLinkerService,
)
from kgforge.specializations.resources.entity_linking_candidate import (
    EntityLinkingCandidate,
)


class EntityLinkerElasticService(EntityLinkerService):
    def __init__(
        self,
        store: Callable,
        targets: Dict[str, str],
        encoder,
        result_resource_mapping,
        **store_config
    ):
        super().__init__(is_distance=False)
        self.sources: Dict[str, Store] = {}
        for identifier in targets:
            bucket = targets[identifier]['bucket']
            store_config.update(bucket=bucket)
            self.sources[identifier] = store(**store_config)
        self.encoder = encoder
        self.result_mapping: Any = self.mapping.load(result_resource_mapping)

    @property
    def mapping(self) -> Callable:
        return DictionaryMapping

    @property
    def mapper(self) -> Callable:
        return DictionaryMapper

    def generate_candidates(
        self, mentions, target, mention_context, limit, bulk
    ) -> Optional[Union[EntityLinkingCandidate, List[EntityLinkingCandidate]]]:
        def _(d, resource):
            return EntityLinkingCandidate(d, **resource)

        mentions_index = [(i, str(mention)) for i, mention in enumerate(mentions)]
        mentions_labels = {str(mention) for mention in mentions}

        resources, scores = [], []
        for mention in mentions_labels:
            call_url = self.encoder.format(x=mention)
            embedding_object = requests.get(url=call_url)
            embedding = self.mapper().map(
                embedding_object.json(), self.result_mapping, None
            )
            if embedding is not None:
                embedding_json = encode(embedding)
                vector_field = list(embedding_json.keys())[0]
                mention_resources, mention_resources_scores = self._similar(
                    vector_field, embedding_json[vector_field], target, limit
                )
                resources.append(mention_resources)
                scores.append(mention_resources_scores)
        i_res = {
            m: [_(scores[j][i], resource) for i, resource in enumerate(rs)]
            for j, (m, rs) in enumerate(
                zip(itertools.cycle(mentions_labels), resources)
            )
        }
        return [(m, i_res[m]) for i, m in mentions_index if m in i_res]

    def _similar(self, vector_field, item_embedding, target, limit, offset=0):
        """
        Given a vector, find similar top [limit] resources, ranked by cosine similarity
        """
        embedding_filter = Filter(
            operator=FilterOperator.EQUAL.value,
            path=[vector_field],
            value=item_embedding,
        )

        resources = self.sources[target].search(
            None,
            embedding_filter,
            limit=limit,
            offset=offset,
            excludes=[vector_field],
            search_endpoint="elastic",
        )

        if len(resources) > 0:
            scores = [
                r._store_metadata._score
                for r in resources
                if hasattr(r._store_metadata, "_score")
            ]

            return (
                as_json(
                    resources,
                    expanded=False,
                    store_metadata=True,
                    model_context=None,
                    metadata_context=None,
                    context_resolver=None,
                ),
                scores,
            )

        return None
