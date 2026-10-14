"""Selectable judge engines for Auditrail runs.

Engines produce an untrusted report proposal. Normalization, evidence
validation, deterministic caps, quality assessment, and report writing stay in
``auditrail.agents.tools.write_judge_report``; no engine may write final report
files directly.
"""
