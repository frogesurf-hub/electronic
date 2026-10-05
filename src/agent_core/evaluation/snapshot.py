"""Chinook 评估快照核验的兼容导入入口。"""
from .chinook_snapshot import ROOT, ChinookSnapshotVerifier

SnapshotVerifier = ChinookSnapshotVerifier
