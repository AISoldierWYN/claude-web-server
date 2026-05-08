"""Android issue analysis backend primitives."""

from .archive import ExtractionLimits, safe_extract_archive
from .casebook import recall_case_cards
from .evidence import generate_first_evidence_pack
from .jobs import AndroidAnalysisJobStore
from .planner import run_planner
from .profiler import profile_extracted_tree
from .reporter import generate_first_report
from .rule_engine import run_rule_matching
from .rule_loader import load_rule_packs
from .sampler import sample_files

__all__ = [
    'AndroidAnalysisJobStore',
    'ExtractionLimits',
    'generate_first_evidence_pack',
    'generate_first_report',
    'load_rule_packs',
    'profile_extracted_tree',
    'recall_case_cards',
    'run_rule_matching',
    'run_planner',
    'sample_files',
    'safe_extract_archive',
]
