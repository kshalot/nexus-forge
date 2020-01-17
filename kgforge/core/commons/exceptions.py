# 
# Knowledge Graph Forge is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# Knowledge Graph Forge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser
# General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with Knowledge Graph Forge. If not, see <https://www.gnu.org/licenses/>.


# Forge operations.

class ConfigurationError(Exception):
    pass


class NotSupportedError(Exception):
    pass


# Model operations.


class ValidationError(Exception):
    pass


# Resolver operations.


class ResolvingError(Exception):
    pass


# Store operations.


class RegistrationError(Exception):
    pass


class UploadingError(Exception):
    pass


class RetrievalError(Exception):
    pass


class DownloadingError(Exception):
    pass


class UpdatingError(Exception):
    pass


class TaggingError(Exception):
    pass


class DeprecationError(Exception):
    pass


class QueryingError(Exception):
    pass


class FreezingError(Exception):
    pass
