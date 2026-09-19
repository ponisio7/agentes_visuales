# tests/test_prompt_builder.py
"""Tests del prompt del planificador (PromptBuilder)."""

from core.problem_solver.prompt_builder import PromptBuilder


def test_prompt_contiene_regla_http_vs_browser():
    prompt = PromptBuilder.build_system_prompt()

    assert "CÓMO ELEGIR ENTRE HTTP Y BROWSER" in prompt


def test_regla_http_vs_browser_es_generica():
    """La regla no debe mencionar casos, dominios ni sitios concretos."""
    prompt = PromptBuilder.build_system_prompt()

    for termino in ("Hacker News", "news.ycombinator", "titulares", "hackernews"):
        assert termino not in prompt

    # Y debe explicar la distinción JSON vs HTML.
    assert "HTTP solo sirve para URLs que devuelven JSON" in prompt
    assert "usa Browser" in prompt
