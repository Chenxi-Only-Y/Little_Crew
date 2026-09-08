"""美术资产部 — 部门页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _dept import render_dept
render_dept("art_dept")
