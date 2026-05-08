"""Shared models and errors for Android issue analysis."""

from __future__ import annotations

from dataclasses import dataclass


class AndroidAnalysisError(Exception):
    """Domain error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExtractionLimits:
    max_archive_size: int = 100 * 1024 * 1024
    max_total_size: int = 300 * 1024 * 1024
    max_file_size: int = 80 * 1024 * 1024
    max_files: int = 5000
    max_depth: int = 24


@dataclass(frozen=True)
class ProfileLimits:
    max_files: int = 10000
    max_depth: int = 32


@dataclass(frozen=True)
class SampleLimits:
    max_files: int = 80
    max_scan_bytes_per_file: int = 8 * 1024 * 1024
    max_chunk_bytes: int = 256 * 1024
    head_lines: int = 40
    tail_lines: int = 40
    context_lines: int = 6
    max_keyword_matches_per_file: int = 8
    max_chars_per_file: int = 20000
