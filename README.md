# Agentes Visuales v3.1

Orquestador de agentes autónomos con backend DeepSeek.

## Instalación

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

## Configuración

    mkdir -p ~/.config/agentes_visuales
    echo 'export DEEPSEEK_API_KEY="sk-tu-key"' > ~/.config/agentes_visuales/env
    chmod 600 ~/.config/agentes_visuales/env

## Uso

    python main.py                    # GUI
    python main.py --check-env        # Diagnóstico
    python run_all_tests.py           # Tests

## Arquitectura

- `core/` — scheduler, sandbox, LLM client, cancellation
- `storage/` — persistencia SQLite (WAL)
- `learning/` — aprendizaje online, A/B testing de prompts
- `ui/` — PyQt6 (terminal retro)
- `export/` — exportadores CSV/JSON/HTML/Excel/Markdown/PDF
- `tools/` — scripts de verificación