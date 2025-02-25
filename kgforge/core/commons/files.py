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
from builtins import all
from urllib.parse import urlparse
from pathlib import Path
import requests
import yaml
from requests import RequestException


def load_yaml_from_file(filepath: str):
    config_data = load_file_as_byte(filepath)
    config_data = config_data.decode("utf-8")
    return yaml.safe_load(config_data)


def load_file_as_byte(source: str):
    # source: Union[str, Path, URL].
    filepath = Path(source)
    if filepath.is_file():
        data = filepath.read_bytes()
    else:
        try:
            response = requests.get(source)
            response.raise_for_status()
            data = response.content
        except RequestException as re:
            raise AttributeError(
                f"Failed to load the configuration from {source}. "
                f"The provided source is not a valid file path or URL: {str(re)}"
            ) from re
    return data


def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False
