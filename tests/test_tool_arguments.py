import pytest

from warmagent.tool_arguments import normalize_arguments


def test_normalizes_unique_case_and_separator_aliases():
    schema = {"type": "object", "properties": {
        "file_path": {"type": "string"},
        "options": {"type": "object", "properties": {
            "max_results": {"type": "integer"}}}}}
    assert normalize_arguments({"File-Path": "a", "OPTIONS": {"MaxResults": 2}}, schema) == {
        "file_path": "a", "options": {"max_results": 2}}


def test_exact_key_with_alias_and_multiple_aliases_are_ambiguous():
    schema = {"type": "object", "properties": {"file_path": {"type": "string"}}}
    with pytest.raises(ValueError):
        normalize_arguments({"file_path": "a", "File-Path": "a"}, schema)
    with pytest.raises(ValueError):
        normalize_arguments({"filePath": "a", "file-path": "a"}, schema)


def test_unknown_keys_are_preserved_for_schema_validation():
    args = {"file_path": "a", "extra_key": 1}
    assert normalize_arguments(args, {"properties": {"file_path": {}}}) == args


def test_exact_key_wins_when_schema_contains_normalization_collision():
    schema = {"type": "object", "properties": {
        "image_size": {"type": "string"}, "imageSize": {"type": "string"}}}
    assert normalize_arguments({"image_size": "exact"}, schema) == {"image_size": "exact"}


def test_alias_is_rejected_when_schema_has_multiple_normalization_candidates():
    schema = {"type": "object", "properties": {
        "image_size": {"type": "string"}, "imageSize": {"type": "string"}}}
    with pytest.raises(ValueError):
        normalize_arguments({"image-size": "ambiguous"}, schema)


def test_alias_conflicts_with_existing_exact_target():
    schema = {"type": "object", "properties": {"file_path": {"type": "string"}}}
    with pytest.raises(ValueError):
        normalize_arguments({"file_path": "exact", "filePath": "alias"}, schema)
