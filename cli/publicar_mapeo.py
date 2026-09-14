#!/usr/bin/env python3
"""
publicar_mapeo.py — Automatiza la entrega de un mapeo de Guyra Agro a un cliente.

Dado un archivo GeoTIFF (o COG ya generado), este script:
  1. Verifica que sea un Cloud Optimized GeoTIFF válido; si no, lo convierte.
  2. Lo sube como asset de un GitHub Release (mismo patrón que ya usás:
     github.com/<repo>/releases/download/<tag>/<archivo>).
  3. Calcula superficie, resolución y genera una miniatura.
  4. Arma el link al visor de Guyra Agro (viewer/index.html) con los datos
     del trabajo cargados.
  5. Genera un PDF de una página con esos datos + un código QR al mapa.

Requiere en la máquina donde se ejecuta:
  - Python 3.9+ con: rasterio, rio-cogeo, fpdf2, pyproj, qrcode[pil]
    (instalar con: pip install rasterio rio-cogeo fpdf2 pyproj "qrcode[pil]")
  - GDAL (viene con rasterio, no hace falta instalarlo aparte)
  - GitHub CLI (`gh`) autenticado: gh auth login

Ver config.json para configurar el repo de GitHub y la URL del visor
(una sola vez).

Uso:
  python3 publicar_mapeo.py archivo.tif \
      --cliente "Estancia La Ejemplo" \
      --trabajo "Lote 12" \
      --tipo rgb \
      --fecha 2026-09-12
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

try:
    import rasterio
    from rasterio.warp import transform_bounds
    from rio_cogeo.cogeo import cog_translate, cog_validate
    from rio_cogeo.profiles import cog_profiles
    from pyproj import Geod
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    import qrcode
    import numpy as np
    from PIL import Image
except ImportError as e:
    print(f"Falta instalar una dependencia: {e}")
    print("Corré: pip install rasterio rio-cogeo fpdf2 pyproj \"qrcode[pil]\" pillow numpy")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

TIPO_LABELS = {
    "rgb": "Ortomosaico RGB",
    "ndvi": "Índice NDVI (vigor vegetativo)",
    "ndre": "Índice NDRE",
    "ndwi": "Índice NDWI (estrés hídrico)",
    "dsm": "Modelo de elevación (DSM)",
}


def cargar_config():
    if not CONFIG_PATH.exists():
        print(f"No encontré {CONFIG_PATH}.")
        print("Copiá config.example.json a config.json y completá:")
        print('  - "github_repo": tu repo de GitHub, formato "usuario/repo"')
        print('  - "viewer_base_url": la URL pública donde publicaste la carpeta viewer/')
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def slugify(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    return re.sub(r"[-\s]+", "-", texto)


def verificar_gh_disponible():
    if shutil.which("gh") is None:
        print("No encontré el comando `gh` (GitHub CLI). Instalalo desde https://cli.github.com/")
        sys.exit(1)
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("El GitHub CLI no está autenticado en esta máquina.")
        print("Corré: gh auth login")
        sys.exit(1)


def asegurar_cog(origen: Path, salida: Path, tipo: str, lossless: bool = False) -> Path:
    """Prepara el COG a subir.

    Para 'rgb' (salvo --lossless), siempre recomprime a JPEG: la subida es el
    cuello de botella real (conexiones lentas), no la fidelidad de pixel, y
    para que un cliente mire el mapa una compresión con pérdida imperceptible
    reduce el archivo entre 5x y 10x. Para índices/DSM nunca se usa JPEG
    (arruinaría los valores que el visor necesita para la rampa de color):
    se deja el archivo tal cual si ya es un COG válido, o se convierte sin
    pérdida (deflate) si no lo es.
    """
    if tipo == "rgb" and not lossless:
        print(f"Generando COG optimizado (JPEG, calidad 90) para '{origen.name}'...")
        with rasterio.open(origen) as src:
            profile = cog_profiles.get("jpeg")
            profile.update(quality=90)
            indexes = (1, 2, 3) if src.count >= 3 else None
            cog_translate(
                src, str(salida), profile,
                indexes=indexes, in_memory=False, quiet=False, add_mask=True,
            )
        ok, errores, _ = cog_validate(str(salida))
        if not ok:
            print(f"No se pudo generar un COG válido: {errores}")
            sys.exit(1)
        origen_mb = origen.stat().st_size / 1e6
        salida_mb = salida.stat().st_size / 1e6
        print(f"Tamaño: {origen_mb:.1f} MB -> {salida_mb:.1f} MB (para subir sin pérdida usá --lossless)")
        return salida

    ok, errores, _ = cog_validate(str(origen))
    if ok:
        print(f"'{origen.name}' ya es un COG válido, no hace falta convertir.")
        return origen

    print(f"'{origen.name}' no es un COG válido ({errores}). Convirtiendo (sin pérdida)...")
    with rasterio.open(origen) as src:
        profile = cog_profiles.get("deflate")
        cog_translate(src, str(salida), profile, in_memory=False, quiet=False)
    ok, errores, _ = cog_validate(str(salida))
    if not ok:
        print(f"No se pudo generar un COG válido: {errores}")
        sys.exit(1)
    print(f"COG generado en: {salida}")
    return salida


def calcular_metadatos(cog_path: Path):
    with rasterio.open(cog_path) as src:
        bounds_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        left, bottom, right, top = bounds_wgs84
        geod = Geod(ellps="WGS84")
        area_m2, _ = geod.polygon_area_perimeter(
            [left, right, right, left], [bottom, bottom, top, top]
        )
        area_ha = abs(area_m2) / 10000.0

        # resolución aproximada en cm/pixel (usando el ancho en x del pixel original)
        px_size_x = abs(src.transform.a)
        if src.crs and src.crs.is_geographic:
            # convertir grados a metros aprox en el centro de la escena
            lat_centro = (bottom + top) / 2
            metros_por_grado = 111320 * abs(np.cos(np.radians(lat_centro)))
            res_cm = px_size_x * metros_por_grado * 100
        else:
            res_cm = px_size_x * 100

        return {
            "bounds_wgs84": (left, bottom, right, top),
            "area_ha": round(area_ha, 2),
            "resolucion_cm": round(res_cm, 1),
            "ancho": src.width,
            "alto": src.height,
            "bandas": src.count,
        }


def generar_miniatura(cog_path: Path, salida_png: Path, tipo: str, max_dim=700):
    with rasterio.open(cog_path) as src:
        escala = max(src.width, src.height) / max_dim
        out_w = max(1, int(src.width / escala))
        out_h = max(1, int(src.height / escala))

        if tipo == "rgb" and src.count >= 3:
            data = src.read([1, 2, 3], out_shape=(3, out_h, out_w))
            arr = np.transpose(data, (1, 2, 0))
            if arr.dtype != np.uint8:
                arr = np.clip(arr / arr.max() * 255, 0, 255).astype(np.uint8) if arr.max() > 0 else arr.astype(np.uint8)
            img = Image.fromarray(arr, mode="RGB")
        else:
            data = src.read(1, out_shape=(out_h, out_w)).astype(np.float32)
            data_norm = data - np.nanmin(data)
            maxv = np.nanmax(data_norm) or 1
            data_norm = (data_norm / maxv * 255).astype(np.uint8)
            img = Image.fromarray(data_norm, mode="L").convert("RGB")

        img.save(salida_png)


def subir_a_github_release(archivo: Path, repo: str, tag: str, titulo: str) -> str:
    """Sube el archivo como asset de un release y devuelve la URL de descarga."""
    verificar_gh_disponible()
    print(f"Subiendo '{archivo.name}' a {repo} (release '{tag}')...")

    existe = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo], capture_output=True, text=True
    )
    if existe.returncode == 0:
        cmd = ["gh", "release", "upload", tag, str(archivo), "--repo", repo, "--clobber"]
    else:
        cmd = [
            "gh", "release", "create", tag, str(archivo),
            "--repo", repo, "--title", titulo, "--notes", "Mapeo generado por Guyra Agro",
        ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("Error subiendo el archivo a GitHub:")
        print(r.stderr)
        sys.exit(1)

    return f"https://github.com/{repo}/releases/download/{tag}/{archivo.name}"


def armar_link_visor(viewer_base_url: str, cog_url: str, cliente: str, trabajo: str,
                      fecha: str, tipo: str, nota: str) -> str:
    params = {"url": cog_url, "cliente": cliente, "trabajo": trabajo, "fecha": fecha, "tipo": tipo}
    if nota:
        params["nota"] = nota
    base = viewer_base_url.rstrip("/")
    sep = "" if base.endswith(".html") else "/"
    return f"{base}{sep}?{urlencode(params)}"


def generar_qr(link: str, salida_png: Path):
    img = qrcode.make(link)
    img.save(salida_png)


def generar_pdf(salida_pdf: Path, cliente: str, trabajo: str, fecha: str, tipo: str,
                 nota: str, metadatos: dict, thumbnail_png: Path, qr_png: Path, link: str):
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    pdf.set_fill_color(27, 94, 32)  # verde Guyra Agro
    pdf.rect(0, 0, 210, 22, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_xy(10, 6)
    pdf.cell(0, 10, "Guyra Agro - Reporte de mapeo")

    pdf.set_text_color(20, 20, 20)
    pdf.set_xy(10, 30)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, cliente, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"{trabajo}  ·  {fecha}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.cell(0, 7, TIPO_LABELS.get(tipo, tipo), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if nota:
        pdf.set_x(10)
        pdf.multi_cell(190, 6, nota)

    pdf.ln(3)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Superficie relevada: {metadatos['area_ha']} ha", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(10)
    pdf.cell(0, 6, f"Resolucion aproximada: {metadatos['resolucion_cm']} cm/pixel", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    img_y = pdf.get_y() + 4
    pdf.image(str(thumbnail_png), x=10, y=img_y, w=120)
    pdf.image(str(qr_png), x=140, y=img_y, w=55)
    pdf.set_xy(140, img_y + 57)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(55, 4, "Escaneá el código para ver el mapa interactivo", align="C")

    pdf.set_y(-20)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 5, f"Link directo: {link}", align="C")

    pdf.output(str(salida_pdf))


def main():
    ap = argparse.ArgumentParser(description="Publica un mapeo de Guyra Agro para un cliente.")
    ap.add_argument("archivo", type=Path, help="Ruta al GeoTIFF/COG a publicar")
    ap.add_argument("--cliente", required=True)
    ap.add_argument("--trabajo", required=True)
    ap.add_argument("--tipo", choices=TIPO_LABELS.keys(), default="rgb")
    ap.add_argument("--fecha", default=date.today().isoformat())
    ap.add_argument("--nota", default="")
    ap.add_argument("--salida", type=Path, default=Path("salida"), help="Carpeta donde dejar COG/PDF generados")
    ap.add_argument("--sin-subir", action="store_true",
                     help="No sube nada a GitHub; solo procesa el archivo localmente (para pruebas)")
    ap.add_argument("--lossless", action="store_true",
                     help="Para --tipo rgb: no recomprimir a JPEG, subir sin pérdida (archivo mucho más pesado)")
    args = ap.parse_args()

    if not args.archivo.exists():
        print(f"No encontré el archivo: {args.archivo}")
        sys.exit(1)

    args.salida.mkdir(parents=True, exist_ok=True)
    slug_cliente = slugify(args.cliente)
    slug_trabajo = slugify(args.trabajo)
    tag = f"{slug_cliente}-{slug_trabajo}-{args.fecha}"

    cog_final = asegurar_cog(args.archivo, args.salida / f"{tag}_{args.tipo}.tif", args.tipo, args.lossless)

    metadatos = calcular_metadatos(cog_final)
    print(f"Superficie: {metadatos['area_ha']} ha | Resolución: {metadatos['resolucion_cm']} cm/px")

    miniatura = args.salida / f"{tag}_miniatura.png"
    generar_miniatura(cog_final, miniatura, args.tipo)

    config = {} if args.sin_subir else cargar_config()

    if args.sin_subir:
        cog_url = f"file://{cog_final.resolve()}"
        print("Modo --sin-subir: no se sube nada a GitHub, se usa una URL local de prueba.")
    else:
        cog_url = subir_a_github_release(
            cog_final, config["github_repo"], tag, f"{args.cliente} - {args.trabajo}"
        )

    viewer_base_url = config.get("viewer_base_url", "http://localhost:8792/index.html")
    link = armar_link_visor(viewer_base_url, cog_url, args.cliente, args.trabajo,
                             args.fecha, args.tipo, args.nota)

    qr_png = args.salida / f"{tag}_qr.png"
    generar_qr(link, qr_png)

    pdf_path = args.salida / f"{tag}_reporte.pdf"
    generar_pdf(pdf_path, args.cliente, args.trabajo, args.fecha, args.tipo,
                args.nota, metadatos, miniatura, qr_png, link)

    print("\nListo.")
    print(f"Link para el cliente: {link}")
    print(f"PDF generado en: {pdf_path.resolve()}")


if __name__ == "__main__":
    main()
