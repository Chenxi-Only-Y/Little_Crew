"""导演分镜部 — 部门页。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _dept import render_dept
render_dept("direction")
