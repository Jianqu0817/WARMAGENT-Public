"""Clone verified JSON archive containers through pydantic-core's Rust serializer.

This is a read-copy operation, not validation of new or untrusted records.
The existing fields, content hash and fields-set metadata remain unchanged.
"""
from pydantic_core import SchemaSerializer, core_schema


_JSON_COPY = SchemaSerializer(core_schema.any_schema())


def clone_json_value(value):
    return _JSON_COPY.to_python(value, mode='python')


def clone_record(record):
    cloned = record.model_copy()
    # These are Record's only mutable JSON fields. model_copy preserves the
    # scalar fields and independent Pydantic fields-set metadata.
    refs, generator, payload = clone_json_value((record.source_refs, record.generator, record.payload))
    cloned.__dict__.update(source_refs=refs, generator=generator, payload=payload)
    return cloned
