# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# try to import to serialize Pydantic Objects
import importlib

import pydantic

from burr.core import serde


@serde.serialize.register(pydantic.BaseModel)
def serialize_pydantic(value: pydantic.BaseModel, **kwargs) -> dict:
    """Uses pydantic to dump the model to a dictionary and then adds the __pydantic_class to the dictionary."""
    _dict = value.model_dump()
    _dict[serde.KEY] = "pydantic"
    # get qualified name of pydantic class. The module name should be fully qualified.
    _dict["__pydantic_class"] = f"{value.__class__.__module__}.{value.__class__.__name__}"
    return _dict


@serde.deserializer.register("pydantic")
def deserialize_pydantic(value: dict, **kwargs) -> pydantic.BaseModel:
    """Deserializes a pydantic object from a dictionary.
    This will pop the __pydantic_class and then import the class.
    """
    value.pop(serde.KEY)
    pydantic_class_name = value.pop("__pydantic_class")
    module_name, class_name = pydantic_class_name.rsplit(".", 1)
    module = importlib.import_module(module_name)
    pydantic_class = getattr(module, class_name)
    return pydantic_class.model_validate(value)
