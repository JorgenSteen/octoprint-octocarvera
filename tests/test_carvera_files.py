"""Tests for carvera_files.py — path encoding and ls response parsing."""

import pytest

from octocarvera.carvera_files import decode_path, encode_path, parse_ls_response


class TestPathEncoding:
    def test_encode_spaces(self):
        assert encode_path("/sd/my file.nc") == "/sd/my\x01file.nc"

    def test_encode_special_chars(self):
        assert encode_path("test?file") == "test\x02file"
        assert encode_path("a&b") == "a\x03b"
        assert encode_path("run!now") == "run\x04now"
        assert encode_path("path~v2") == "path\x05v2"

    def test_encode_no_special_chars(self):
        assert encode_path("/sd/simple.nc") == "/sd/simple.nc"

    def test_decode_roundtrip(self):
        original = "/sd/my file?v2.nc"
        assert decode_path(encode_path(original)) == original

    def test_decode_spaces(self):
        assert decode_path("my\x01file.nc") == "my file.nc"


class TestParseLsResponse:
    def test_file_entry(self):
        lines = ["myfile.nc 12345 20240315143022"]
        result = parse_ls_response(lines)
        assert len(result) == 1
        assert result[0]["name"] == "myfile.nc"
        assert result[0]["is_dir"] is False
        assert result[0]["size"] == 12345
        assert result[0]["date"] == "2024-03-15 14:30"

    def test_directory_entry(self):
        lines = ["subfolder/ 0 20240312000000"]
        result = parse_ls_response(lines)
        assert len(result) == 1
        assert result[0]["name"] == "subfolder"
        assert result[0]["is_dir"] is True
        assert result[0]["size"] == 0

    def test_multiple_entries(self):
        lines = [
            "4thaxis/ 0 20240101120000",
            "Tests/ 0 20240201120000",
            "Examples/ 0 20240301120000",
        ]
        result = parse_ls_response(lines)
        assert len(result) == 3
        # Sorted alphabetically (all dirs)
        assert result[0]["name"] == "4thaxis"
        assert result[1]["name"] == "Examples"
        assert result[2]["name"] == "Tests"

    def test_mixed_files_and_dirs(self):
        lines = [
            "readme.txt 100 20240101120000",
            "subfolder/ 0 20240101120000",
        ]
        result = parse_ls_response(lines)
        # Directories first
        assert result[0]["name"] == "subfolder"
        assert result[0]["is_dir"] is True
        assert result[1]["name"] == "readme.txt"
        assert result[1]["is_dir"] is False

    def test_soh_encoded_filename(self):
        lines = ["my\x01file.nc 500 20240315143022"]
        result = parse_ls_response(lines)
        assert result[0]["name"] == "my file.nc"

    def test_hidden_dirs_filtered(self):
        lines = [
            ".md5/ 0 20240101120000",
            ".lz/ 0 20240101120000",
            "realfolder/ 0 20240101120000",
        ]
        result = parse_ls_response(lines)
        assert len(result) == 1
        assert result[0]["name"] == "realfolder"

    def test_empty_lines_skipped(self):
        lines = ["", "  ", "file.nc 100 20240101120000", ""]
        result = parse_ls_response(lines)
        assert len(result) == 1

    def test_ok_line_skipped_in_list_files(self):
        """The 'ok' line is filtered in list_files(), not parse_ls_response()."""
        # parse_ls_response should handle lines that don't match the format
        lines = ["ok"]
        result = parse_ls_response(lines)
        assert len(result) == 0  # "ok" has <3 parts after rsplit

    def test_empty_input(self):
        assert parse_ls_response([]) == []

    def test_short_date_passthrough(self):
        lines = ["file.nc 100 2024"]
        result = parse_ls_response(lines)
        assert result[0]["date"] == "2024"  # Too short, passed through as-is
