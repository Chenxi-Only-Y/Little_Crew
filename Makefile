# 常用任务入口（2026-11-12）
#
# 为什么要 Makefile：现在开跑前要记一堆命令（起服务、自检、跑集、清理、报告），
# 而 Windows 上直接敲这些命令很容易漏参数。统一成 `make xxx`（或 `make.ps1 xxx`）。
#
# Windows 没装 make 时用 `.\make.ps1 xxx`（内容一一对应）。

PY ?= D:/Python/python.exe
PORT ?= 8787
EP ?= EF40
WD ?= outputs/_$(EP)

.PHONY: help doctor test serve episode lib report prune clean fmt check

help:
	@echo "make doctor    环境自检（服务/key/模型/磁盘/ffmpeg）"
	@echo "make test      全量测试（2447 项 + 架构守卫）"
	@echo "make serve     起对外 API（端口 $(PORT)）"
	@echo "make episode   跑一集（EP=$(EP)，目录 $(WD)）"
	@echo "make lib       列出跑过的每一集"
	@echo "make report    生成 $(WD) 的报告页"
	@echo "make prune     outputs 超过 2GB 才深度清理"
	@echo "make check     doctor + test 一起跑"

doctor:
	$(PY) -m little_crew.cli doctor

test:
	$(PY) tests/run_all.py

serve:
	$(PY) -m little_crew.cli serve --port $(PORT)

episode:
	$(PY) -m little_crew.runtime.episode_demo --llm --accept-script \
	  --voice --final --vision-review --backend comfyui --use-white-model \
	  --comfy-input "$(COMFY_INPUT)" --episode-id $(EP) --workdir $(WD)

lib:
	$(PY) -m little_crew.cli library

report:
	$(PY) -m little_crew.cli report $(WD)

prune:
	$(PY) tools/prune_artifacts.py --auto --outputs outputs --max-gb 2.0

check: doctor test
