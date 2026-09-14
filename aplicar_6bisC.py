from pathlib import Path

p = Path("core/problem_solver.py")
src = p.read_text(encoding="utf-8")

if "obtener_lecciones_para_prompt" in src:
    print("Ya estaba aplicado, no toco nada.")
    raise SystemExit(0)

viejo = """        # 2. Consultar al LLM (con reintentos y modelos alternativos)
        plan_dict = self._consultar_llm_con_reintentos(user_prompt)
"""

nuevo = """        # ✅ NUEVO (6-bis-C): Inyectar lecciones aprendidas en el user_prompt
        try:
            from learning import obtener_learning_engine
            engine = obtener_learning_engine()
            if engine is not None:
                bloque_lecciones = engine.obtener_lecciones_para_prompt()
                if bloque_lecciones:
                    user_prompt = (
                        user_prompt
                        + "\\n\\n"
                        + bloque_lecciones
                    )
                    self.logger.info(
                        "🧠 Lecciones aprendidas inyectadas en el prompt"
                    )
        except Exception as e:
            self.logger.debug(f"Learning no disponible: {e}")

        # 2. Consultar al LLM (con reintentos y modelos alternativos)
        plan_dict = self._consultar_llm_con_reintentos(user_prompt)
"""

if viejo not in src:
    print("❌ No encontré el bloque exacto. Nada modificado.")
    raise SystemExit(1)

# Backup antes de tocar
backup = p.with_suffix(".py.bak")
backup.write_text(src, encoding="utf-8")
print(f"📦 Backup: {backup}")

src = src.replace(viejo, nuevo, 1)
p.write_text(src, encoding="utf-8")
print("✅ Bloque 6-bis-C aplicado")
