# 001 — Project scaffold  (model: haiku · wave 1)

Read `plans/000-interfaces.md` first.

## Deliverables
1. `pyproject.toml`:
   - project `sqlmpeg`, version 0.1.0, `requires-python = ">=3.10"`, deps: `["sqlglot"]`.
   - description: "SQL frontend for FFmpeg filtergraphs".
   - `[project.scripts] sqlmpeg = "sqlmpeg.cli:main"`.
   - optional-dependencies `dev = ["pytest", "ruff", "mypy", "hypothesis", "imagehash"]`.
   - build-system: setuptools.
   - `[tool.ruff]` line-length 100; `[tool.ruff.lint]` select E, F, I, UP.
   - `[tool.mypy]` strict = true, python_version = "3.10".
2. `sqlmpeg/__init__.py`: docstring + `__version__ = "0.1.0"`. Nothing else.
3. `sqlmpeg/py.typed` (empty marker).
4. `README.md`: title, one-paragraph pitch, and the SQL→ffmpeg example copied verbatim
   from `sqlmpeg-project.md` lines 7–21. Add a loud "Audio: v0 copies audio from the
   first input (`-c:a copy`); SQL is video-only." note. Mark as work-in-progress.
5. `tests/__init__.py` empty file.

## Verify
`D:\projects\sqlmpeg\.venv\Scripts\python.exe -m pip install -e .` succeeds, then
`python -c "import sqlmpeg; print(sqlmpeg.__version__)"` prints 0.1.0.

## Do NOT
Create any other module files. Do not git commit.
