"""Regression test: a malformed tool result must fail loudly at the
schema boundary, not get silently serialized and passed downstream.

Two independent layers are supposed to catch this, and this test proves
both directly instead of assuming either works:

  1. server.py's own `_build_matched_result`/`_build_ambiguous_result`/
     `_build_download_result` construct a Pydantic model before ever
     returning a dict -- a missing required field, wrong type, or a
     constraint violation (e.g. a negative `size_bytes`) raises
     `pydantic.ValidationError` right there, before any malformed data
     is serialized.
  2. The declared `outputSchema` on each `Tool` (generated from the same
     Pydantic models via `model_json_schema()`) is what the MCP SDK's
     own low-level `Server.call_tool` wrapper validates
     `structuredContent` against via `jsonschema.validate` -- a second,
     independent, protocol-level check that would catch the same
     malformed payload even if the Pydantic layer were somehow bypassed.

Runs in-process, no live Drive/OAuth/Cerebras account needed: this is a
pure schema-boundary question ("does bad data get rejected"), not a
question the wire protocol needs to answer.
"""

import jsonschema
import pydantic
import server


def test_valid_matched_result_round_trips() -> None:
    result = server._build_matched_result(
        [{"file_id": "1abc", "name": "Refund Policy", "mimeType": "application/vnd.google-apps.document"}]
    )
    assert result == {
        "status": "matched",
        "matches": [{"file_id": "1abc", "name": "Refund Policy", "mimeType": "application/vnd.google-apps.document"}],
        "message": None,
    }
    jsonschema.validate(instance=result, schema=server.SEARCH_DRIVE_FILES_OUTPUT_SCHEMA)


def test_malformed_search_result_rejected_by_pydantic() -> None:
    # Missing 'name' and 'mimeType' -- a plausible real bug (e.g. a typo'd
    # dict key upstream), not a contrived one.
    try:
        server._build_matched_result([{"file_id": "1abc"}])
    except pydantic.ValidationError as exc:
        assert "name" in str(exc) and "mimeType" in str(exc)
    else:
        raise AssertionError("expected pydantic.ValidationError for a candidate missing required fields")


def test_malformed_search_result_also_rejected_by_output_jsonschema() -> None:
    # Simulates the Pydantic layer being bypassed: a hand-built dict, the
    # same shape a bug could produce, checked directly against the
    # declared outputSchema the SDK itself validates structuredContent
    # against -- proving the second, independent layer also rejects it,
    # not just the first.
    malformed = {"status": "matched", "matches": [{"file_id": "1abc"}]}  # missing name/mimeType
    try:
        jsonschema.validate(instance=malformed, schema=server.SEARCH_DRIVE_FILES_OUTPUT_SCHEMA)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("expected jsonschema.ValidationError for a candidate missing required fields")


def test_malformed_search_result_wrong_type_rejected() -> None:
    # 'matches' must be a list of objects, not a bare string.
    try:
        server.SearchDriveFilesResult(status="matched", matches="not-a-list")  # type: ignore[arg-type]
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("expected pydantic.ValidationError for matches being a string, not a list")


def test_valid_download_result_round_trips() -> None:
    result = server._build_download_result(
        file_id="1fi_", name="UCLA&MIT.pdf", mime_type="application/pdf", size_bytes=1903362
    )
    assert result == {"file_id": "1fi_", "name": "UCLA&MIT.pdf", "mimeType": "application/pdf", "size_bytes": 1903362}
    jsonschema.validate(instance=result, schema=server.DOWNLOAD_DRIVE_FILE_OUTPUT_SCHEMA)


def test_negative_size_bytes_rejected() -> None:
    # A malformed byte count is exactly the kind of bug this schema
    # boundary exists to catch loudly instead of quietly relaying a
    # nonsensical number downstream (see DownloadDriveFileResult's
    # docstring).
    try:
        server._build_download_result(file_id="1fi_", name="x.pdf", mime_type="application/pdf", size_bytes=-5)
    except pydantic.ValidationError as exc:
        assert "size_bytes" in str(exc)
    else:
        raise AssertionError("expected pydantic.ValidationError for a negative size_bytes")


def test_negative_size_bytes_also_rejected_by_output_jsonschema() -> None:
    malformed = {"file_id": "1fi_", "name": "x.pdf", "mimeType": "application/pdf", "size_bytes": -5}
    try:
        jsonschema.validate(instance=malformed, schema=server.DOWNLOAD_DRIVE_FILE_OUTPUT_SCHEMA)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("expected jsonschema.ValidationError for a negative size_bytes")


if __name__ == "__main__":
    test_valid_matched_result_round_trips()
    print("OK: a well-formed search result validates and round-trips through the schema")
    test_malformed_search_result_rejected_by_pydantic()
    print("OK: a search result missing required fields is rejected by Pydantic before it's ever returned")
    test_malformed_search_result_also_rejected_by_output_jsonschema()
    print("OK: the same malformed search result is independently rejected by the declared outputSchema")
    test_malformed_search_result_wrong_type_rejected()
    print("OK: a wrong-typed 'matches' field is rejected by Pydantic")
    test_valid_download_result_round_trips()
    print("OK: a well-formed download result validates and round-trips through the schema")
    test_negative_size_bytes_rejected()
    print("OK: a negative size_bytes is rejected by Pydantic before it's ever returned")
    test_negative_size_bytes_also_rejected_by_output_jsonschema()
    print("OK: the same negative size_bytes is independently rejected by the declared outputSchema")
