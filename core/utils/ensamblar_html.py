# core/utils/ensamblar_html.py
import json
import logging
import re
logger = logging.getLogger(__name__)
def _es_documento_completo(s: str) -> bool:
    s_low = s.lower().lstrip()
    return s_low.startswith('<!doctype') or s_low.startswith('<html')
def _corregir_sintaxis_python_en_js(js_code: str):
    if not js_code:
        return js_code, []
    correcciones = []
    patrones = [
        (r'\bNone\b', 'null', 'None→null'),
        (r'\bTrue\b', 'true', 'True→true'),
        (r'\bFalse\b', 'false', 'False→false'),
    ]
    for patron, reemplazo, descripcion in patrones:
        nuevo, n = re.subn(patron, reemplazo, js_code)
        if n > 0:
            js_code = nuevo
            correcciones.append(f'{descripcion} ({n})')
    return js_code, correcciones
def extraer_fragmento(s: str, etiqueta_contenedora: str):
    if not isinstance(s, str):
        return '', False
    s = s.strip()
    s = re.sub(r'^```(?:html|css|javascript|js|xml)?\s*', '', s, flags=re.I)
    s = re.sub(r'\s*```$', '', s)
    if s.startswith('{'):
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                for clave in ('html', 'css', 'js', 'javascript', 'contenido', 'codigo'):
                    if clave in data and isinstance(data[clave], str):
                        s = data[clave]
                        break
        except (json.JSONDecodeError, ValueError):
            pass
    if etiqueta_contenedora == 'style':
        m = re.search(r'<style[^>]*>(.*?)</style>', s, re.S | re.I)
        if m:
            s = m.group(1)
    elif etiqueta_contenedora == 'script':
        m = re.search(r'<script[^>]*>(.*?)</script>', s, re.S | re.I)
        if m:
            s = m.group(1)
    elif etiqueta_contenedora == 'body':
        era_documento_completo = _es_documento_completo(s)
        if era_documento_completo:
            m = re.search(r'<body[^>]*>(.*?)</body>', s, re.S | re.I)
            if m:
                s = m.group(1)
            else:
                s = re.sub(r'<head.*?</head>', '', s, flags=re.S | re.I)
                s = re.sub(r'<!DOCTYPE[^>]*>', '', s, flags=re.I)
                s = re.sub(r'</?html[^>]*>', '', s, flags=re.I)
            s = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.S | re.I)
            s = re.sub(r'<style[^>]*>.*?</style>', '', s, flags=re.S | re.I)
        return s.strip(), era_documento_completo
    return s.strip(), False
def extraer_respuesta(d, claves=('respuesta_limpia', 'respuesta', 'contenido', 'html', 'css', 'js', 'codigo')):
    if isinstance(d, str):
        return d
    if not isinstance(d, dict):
        return ''
    for k in claves:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    anidado = d.get('json')
    if isinstance(anidado, dict):
        for k in claves:
            v = anidado.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ''
def ensamblar_calculadora_html(html_llm, css_llm, js_llm, titulo="Calculadora Científica HP 50"):
    html_raw = extraer_respuesta(html_llm)
    css_raw = extraer_respuesta(css_llm, ('respuesta_limpia', 'respuesta', 'css', 'contenido', 'codigo'))
    js_raw = extraer_respuesta(js_llm, ('respuesta_limpia', 'respuesta', 'js', 'javascript', 'contenido', 'codigo'))
    html_body, html_era_doc_completo = extraer_fragmento(html_raw, 'body')
    css_code, _ = extraer_fragmento(css_raw, 'style')
    js_code, _ = extraer_fragmento(js_raw, 'script')
    if not html_body or not css_code or not js_code:
        raise ValueError('Alguna de las partes generadas esta vacia: html=%d css=%d js=%d' % (len(html_body), len(css_code), len(js_code)))
    if html_era_doc_completo:
        raise ValueError('GenerarEstructuraHTML devolvio un documento completo (<!DOCTYPE>/<html>) en lugar de un fragmento.')
    if re.search(r'\{"html"\s*:', html_body):
        raise ValueError('Se encontro JSON crudo {"html": ...} en el fragmento HTML')
    if re.search(r'<!DOCTYPE', html_body, re.I):
        raise ValueError('Residuo de <!DOCTYPE> en el fragmento HTML tras la extraccion')
    if re.search(r'<html\b', html_body, re.I):
        raise ValueError('Residuo de <html> en el fragmento HTML tras la extraccion')
    if js_code:
        js_code, correcciones_js = _corregir_sintaxis_python_en_js(js_code)
        if correcciones_js:
            logger.warning(f'JS auto-corregido: {", ".join(correcciones_js)}. El LLM no respetó la regla de sintaxis JavaScript.')
    if js_raw.lower().count('<script') > 1:
        logger.warning('GenerarJS devolvio multiples <script>; se usara solo el primero.')
    return ('<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>' + titulo + '</title>\n<style>\n' + css_code + '\n</style>\n</head>\n<body>\n<div class="calculadora">\n' + html_body + '\n</div>\n<script>\n' + js_code + '\n</script>\n</body>\n</html>')