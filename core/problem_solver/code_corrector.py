# core/problem_solver/code_corrector.py
"""
PythonCodeCorrector: corrección automática de código Python generado por el LLM.

El LLM tiene prohibido (ver ``prompt_builder.PYTHON_RULES``) usar variables
sueltas como ``respuesta``, ``data``, ``result`` o ``response``: los
resultados de las dependencias viven en el diccionario ``contexto``. Cuando
las usa igualmente, este corrector las resuelve por NOMBRE SEMÁNTICO contra
la dependencia declarada.

Decisiones de diseño (por qué ya no es una lista de regex):

1. **Consciente de ámbito (AST).** Solo se reescriben ``ast.Name`` en
   contexto ``Load`` que NO estén ligados en ninguna parte del código. Así
   no se rompen ``def f(respuesta)``, ``{'respuesta': 1}``,
   ``print(result)`` ni variables ya asignadas.
2. **Valor real, no placeholder.** La sustitución es
   ``dependencia(contexto, 'Dep')``, un helper del contrato de runtime del
   sandbox (``core/sandbox_contract.py``) que devuelve el valor útil del
   resultado. Nunca un ``.get('body', '{}')`` con forma de HTTP que
   devolvía el string ``'{}'`` cuando la dependencia no era HTTP.
3. **Sin dependencia no se inventa nada.** Si el paso no declara
   dependencias, el nombre queda intacto: es un error real del plan y el
   validador lo rechaza antes de ejecutar.
4. **``json.loads(alias)``** se resuelve como
   ``dependencia(contexto, 'Dep', 'json')`` (JSON ya parseado), evitando el
   doble parseo.
"""
import ast
import builtins
import logging

from core.sandbox_contract import NOMBRES_INYECTADOS

logger = logging.getLogger(__name__)

# Dunders que Python define en cualquier módulo y que nunca hay que reescribir.
_DUNDERS = frozenset({
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__dict__",
})

_CONOCIDOS = frozenset(builtins.dir(builtins)) | set(NOMBRES_INYECTADOS) | _DUNDERS

# Alias que el LLM usa para "la respuesta/el dato útil" de su dependencia.
ALIAS_TEXTO = frozenset({"respuesta", "response", "result", "data"})
# Alias que el LLM usa para "el JSON ya parseado" de su dependencia.
ALIAS_JSON = frozenset({"json_data", "respuesta_json"})
# Alias del contexto completo.
ALIAS_CONTEXTO = frozenset({"dependencias"})


class PythonCodeCorrector:
    """
    Corrige automáticamente código Python generado por LLM.

    El contrato entre agentes se resuelve por nombre semántico:
    ``respuesta`` -> ``dependencia(contexto, 'NombreDependencia')``.
    """

    # Documentación de los alias soportados (compatibilidad con quien lea
    # esta tabla). La corrección real es AST, no regex.
    PATTERNS = [
        {
            "alias": sorted(ALIAS_TEXTO),
            "destino": "dependencia(contexto, '{dep}')",
            "description": "valor principal de la dependencia declarada",
        },
        {
            "alias": sorted(ALIAS_JSON),
            "destino": "dependencia(contexto, '{dep}', 'json')",
            "description": "JSON ya parseado de la dependencia declarada",
        },
        {
            "alias": sorted(ALIAS_CONTEXTO),
            "destino": "contexto",
            "description": "el diccionario de contexto completo",
        },
    ]

    @classmethod
    def corregir(cls, codigo: str, dependencias: list[str]) -> str:
        """Corrige el código Python resolviendo alias por nombre semántico.

        Args:
            codigo: código generado por el LLM.
            dependencias: nombres de las dependencias declaradas por el paso
                (el primero se usa como origen semántico del alias).

        Returns:
            El código corregido. Si no es parseable, se devuelve intacto
            (el validador reporta el ``SyntaxError``).
        """
        if not codigo or not codigo.strip():
            return codigo

        try:
            arbol = ast.parse(codigo)
        except SyntaxError:
            return codigo

        dependencia = dependencias[0] if dependencias else None
        ligados = cls._nombres_ligados(arbol)
        reemplazos: list[tuple[int, int, int, int, str]] = []
        manejados: set[int] = set()

        # 1. json.loads(<alias>) -> dependencia(contexto, 'Dep', 'json')
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not cls._es_json_loads(nodo):
                continue
            if len(nodo.args) != 1 or not isinstance(nodo.args[0], ast.Name):
                continue
            nombre = nodo.args[0].id
            if not cls._alias_resoluble(nombre, ligados, dependencia):
                continue
            reemplazos.append(cls._span(
                nodo, f"dependencia(contexto, {dependencia!r}, 'json')"
            ))
            manejados.add(id(nodo.args[0]))

        # 2. Cualquier alias suelto
        for nodo in ast.walk(arbol):
            if id(nodo) in manejados:
                continue
            if not isinstance(nodo, ast.Name) or not isinstance(nodo.ctx, ast.Load):
                continue
            nombre = nodo.id
            if nombre in ligados or nombre in _CONOCIDOS:
                continue

            if nombre in ALIAS_CONTEXTO:
                nuevo = "contexto"
            elif nombre in ALIAS_JSON:
                if not dependencia:
                    continue
                nuevo = f"dependencia(contexto, {dependencia!r}, 'json')"
            elif nombre in ALIAS_TEXTO:
                if not dependencia:
                    continue
                nuevo = f"dependencia(contexto, {dependencia!r})"
            else:
                continue

            reemplazos.append(cls._span(nodo, nuevo))
            logger.info(
                "🔧 Variable '%s' → %s", nombre, nuevo
            )

        if not reemplazos:
            return codigo
        corregido = cls._aplicar_reemplazos(codigo, reemplazos)
        return cls._validar_o_revertir(codigo, corregido)

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _validar_o_revertir(original: str, corregido: str) -> str:
        """Gate de sintaxis: una corrección que no compila se descarta (3.8).

        El corrector reescribe por spans de texto calculados sobre el AST; si un
        span sale mal, el resultado puede no ser Python válido. El caso histórico
        descrito en el acta era ``data = contexto if contexto else {}``
        convertido en algo como ``contexto contexto...``.

        Aceptar esa corrección rompería el paso en tiempo de ejecución y, peor,
        el error aparecería lejos de su causa. Por eso se comprueba con
        ``compile()`` —el equivalente en proceso de ``py_compile``— y, si falla,
        se devuelve el código **original**: rollback, nunca una corrección a
        medias.
        """
        try:
            compile(corregido, "<python_code_corrector>", "exec")
        except SyntaxError as e:
            logger.warning(
                "↩️ Corrección descartada: el resultado no compila (%s, línea %s). "
                "Se conserva el código original.",
                e.msg, e.lineno,
            )
            return original
        return corregido

    @staticmethod
    def _alias_resoluble(nombre: str, ligados: set, dependencia: str | None) -> bool:
        """True si ``nombre`` es un alias de dependencia sin resolver."""
        if not dependencia:
            return False
        if nombre in ligados or nombre in _CONOCIDOS:
            return False
        return nombre in ALIAS_TEXTO or nombre in ALIAS_JSON

    @staticmethod
    def _es_json_loads(nodo: ast.Call) -> bool:
        """True si la llamada es ``json.loads(...)``."""
        funcion = nodo.func
        return (
            isinstance(funcion, ast.Attribute)
            and funcion.attr == "loads"
            and isinstance(funcion.value, ast.Name)
            and funcion.value.id == "json"
        )

    @staticmethod
    def _span(nodo: ast.AST, texto: str) -> tuple[int, int, int, int, str]:
        """Devuelve (lineno, col, end_lineno, end_col, texto) de un nodo."""
        return (
            nodo.lineno, nodo.col_offset,
            nodo.end_lineno, nodo.end_col_offset,
            texto,
        )

    @staticmethod
    def _nombres_ligados(arbol: ast.AST) -> set:
        """Nombres que el propio código define en cualquier parte.

        Es deliberadamente conservador (ámbito global, no por función): si un
        nombre se asigna en algún sitio, no lo tocamos. Favorece no romper
        código correcto frente a corregir un caso raro.
        """
        ligados: set = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, (ast.Store, ast.Del)):
                ligados.add(nodo.id)
            elif isinstance(nodo, ast.arg):
                ligados.add(nodo.arg)
            elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                ligados.add(nodo.name)
            elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
                ligados.add(nodo.name)
            elif isinstance(nodo, ast.alias):
                ligados.add((nodo.asname or nodo.name).split(".")[0])
            elif isinstance(nodo, (ast.Global, ast.Nonlocal)):
                ligados.update(nodo.names)
        return ligados

    @staticmethod
    def _aplicar_reemplazos(
        codigo: str,
        reemplazos: list[tuple[int, int, int, int, str]],
    ) -> str:
        """Aplica los reemplazos por posiciones, de atrás hacia delante."""
        lineas = codigo.splitlines(keepends=True)
        # Orden inverso: los offsets de los nodos anteriores siguen válidos.
        reemplazos_ordenados = sorted(
            reemplazos, key=lambda r: (r[0], r[1]), reverse=True
        )
        for lineno, col, end_lineno, end_col, texto in reemplazos_ordenados:
            if lineno == end_lineno:
                linea = lineas[lineno - 1]
                lineas[lineno - 1] = linea[:col] + texto + linea[end_col:]
            else:
                prefijo = lineas[lineno - 1][:col]
                sufijo = lineas[end_lineno - 1][end_col:]
                lineas[lineno - 1:end_lineno] = [prefijo + texto + sufijo]
        return "".join(lineas)
