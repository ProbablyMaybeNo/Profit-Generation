import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import llm_codegen as lc  # noqa: E402


# ---------- Prompt construction ----------

def test_prompt_includes_few_shot_examples():
    p = lc.build_prompt("compute_x", entry_rules="long when X", exit_rules="exit when Y")
    assert "compute_5day_low" in p
    assert "compute_3bar_low" in p
    assert "compute_rsi2_oversold" in p
    assert "long when X" in p
    assert "exit when Y" in p
    assert "compute_x" in p


def test_prompt_handles_empty_rules():
    p = lc.build_prompt("compute_x", entry_rules="", exit_rules="")
    assert "(unspecified)" in p


# ---------- Code extraction ----------

def test_extract_code_strips_markdown_fence():
    raw = "Here you go:\n\n```python\ndef compute_foo(df):\n    return df\n```\n\nDone."
    code = lc.extract_code(raw, "compute_foo")
    assert "```" not in code
    assert code.startswith("def compute_foo(")


def test_extract_code_handles_no_fence():
    raw = "def compute_foo(df):\n    return df\n"
    code = lc.extract_code(raw, "compute_foo")
    assert code.startswith("def compute_foo(")


def test_extract_code_pulls_in_immediate_imports():
    raw = "import pandas as pd\nimport numpy as np\n\ndef compute_foo(df):\n    return df\n"
    code = lc.extract_code(raw, "compute_foo")
    assert "import pandas as pd" in code
    assert "import numpy as np" in code


def test_extract_code_drops_chatter_before_imports():
    raw = "Sure! Here's the code:\n\nimport pandas as pd\n\ndef compute_foo(df):\n    return df\n"
    code = lc.extract_code(raw, "compute_foo")
    # Chatter before imports must not survive
    assert "Sure" not in code
    assert "Here" not in code


def test_extract_code_raises_when_function_missing():
    with pytest.raises(ValueError, match="not found"):
        lc.extract_code("def something_else(df): return df", "compute_foo")


# ---------- AST validation ----------

GOOD = """
def compute_foo(df):
    out = df.copy()
    out["long_entry"] = (df["close"] < df["close"].shift(1)).fillna(False)
    out["long_exit"] = (df["close"] > df["close"].shift(1)).fillna(False)
    return out
"""


def test_validate_ast_accepts_clean_code():
    lc.validate_ast(GOOD)


def test_validate_ast_rejects_eval():
    code = GOOD + '\neval("1+1")\n'
    with pytest.raises(ValueError, match="forbidden name: eval"):
        lc.validate_ast(code)


def test_validate_ast_rejects_exec():
    code = GOOD + '\nexec("x=1")\n'
    with pytest.raises(ValueError, match="forbidden name: exec"):
        lc.validate_ast(code)


def test_validate_ast_rejects_dunder_import():
    code = GOOD + '\n__import__("os")\n'
    with pytest.raises(ValueError, match="__import__"):
        lc.validate_ast(code)


def test_validate_ast_rejects_open():
    code = GOOD + '\nopen("/etc/passwd")\n'
    with pytest.raises(ValueError, match="forbidden name: open"):
        lc.validate_ast(code)


def test_validate_ast_rejects_os_import():
    code = "import os\n" + GOOD
    with pytest.raises(ValueError, match="forbidden import"):
        lc.validate_ast(code)


def test_validate_ast_rejects_subprocess_import():
    code = "import subprocess\n" + GOOD
    with pytest.raises(ValueError, match="forbidden import"):
        lc.validate_ast(code)


def test_validate_ast_rejects_dunder_attr():
    code = "import pandas as pd\n" + GOOD + '\nx = (1).__class__\n'
    with pytest.raises(ValueError, match="dunder"):
        lc.validate_ast(code)


def test_validate_ast_accepts_pandas_numpy_math():
    code = "import pandas as pd\nimport numpy as np\nimport math\n" + GOOD
    lc.validate_ast(code)


def test_validate_ast_rejects_syntax_error():
    with pytest.raises(ValueError, match="syntax error"):
        lc.validate_ast("def compute_x(df:\n    return df\n")


# ---------- Smoke test ----------

def test_smoke_accepts_known_good_function():
    fn = lc.smoke_test(GOOD, "compute_foo")
    assert callable(fn)


def test_smoke_rejects_missing_function():
    with pytest.raises(ValueError, match="not defined"):
        lc.smoke_test("x = 1\n", "compute_foo")


def test_smoke_rejects_wrong_return_type():
    code = "def compute_x(df):\n    return 42\n"
    with pytest.raises(ValueError, match="DataFrame"):
        lc.smoke_test(code, "compute_x")


def test_smoke_rejects_missing_long_entry_column():
    code = """
def compute_x(df):
    out = df.copy()
    out["long_exit"] = False
    return out
"""
    with pytest.raises(ValueError, match="long_entry"):
        lc.smoke_test(code, "compute_x")


def test_smoke_rejects_non_boolean_columns():
    code = """
def compute_x(df):
    out = df.copy()
    out["long_entry"] = 1.0
    out["long_exit"] = 0.0
    return out
"""
    with pytest.raises(ValueError, match="boolean"):
        lc.smoke_test(code, "compute_x")


def test_smoke_rejects_exception_in_function():
    code = """
def compute_x(df):
    raise RuntimeError("kaboom")
"""
    with pytest.raises(ValueError, match="kaboom"):
        lc.smoke_test(code, "compute_x")


# ---------- End-to-end with mocked Ollama ----------

def _mock_ollama_response(text: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"response": text}
    return r


def test_generate_end_to_end_with_mock(monkeypatch):
    response_code = """
def compute_test_strat(df):
    out = df.copy()
    lowest_5 = df["low"].rolling(5).min().shift(1)
    out["long_entry"] = (df["close"] < lowest_5).fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out
"""
    monkeypatch.setattr(lc, "_ollama_post",
                        lambda url, payload, timeout: _mock_ollama_response(response_code))
    code = lc.generate_compute_fn(
        "compute_test_strat",
        entry_rules="long when close < 5-bar low",
        exit_rules="exit when close > prev high",
    )
    assert "def compute_test_strat" in code
    assert "long_entry" in code


def test_generate_rejects_unsafe_llm_output(monkeypatch):
    monkeypatch.setattr(lc, "_ollama_post",
                        lambda url, payload, timeout: _mock_ollama_response(
                            "import os\n" + GOOD))
    with pytest.raises(ValueError, match="forbidden import"):
        lc.generate_compute_fn(
            "compute_foo", entry_rules="x", exit_rules="y",
        )


def test_generate_handles_ollama_error(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "internal error"
    monkeypatch.setattr(lc, "_ollama_post", lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama 500"):
        lc.generate_compute_fn(
            "compute_foo", entry_rules="x", exit_rules="y",
        )


# ---------- Helpers ----------

def test_fn_name_from_strategy_id_simple():
    assert lc.fn_name_from_strategy_id("rsi2-oversold") == "compute_rsi2_oversold"


def test_fn_name_from_strategy_id_keeps_compute_prefix():
    assert lc.fn_name_from_strategy_id("compute_already") == "compute_already"


def test_fn_name_from_strategy_id_strips_special_chars():
    assert lc.fn_name_from_strategy_id("botnet101-buy-5day/low!") == "compute_botnet101_buy_5day_low"
