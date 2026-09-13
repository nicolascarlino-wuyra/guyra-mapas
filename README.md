# Guyra Agro — Mapas para clientes

Automatiza lo que ya hacías a mano con cogeo.org: tomar un ortomosaico (COG),
subirlo a algún lado y mandarle el link al cliente. Este paquete separa esa
tarea de agrogestion.html/Supabase (tal como pediste): es un sistema aparte,
solo para publicar mapeos.

Trae dos partes:

- **`viewer/`** — Un visor propio, con la marca de Guyra Agro, que reemplaza
  el link crudo de cogeo.org. Es una sola página HTML, sin backend: lee el
  archivo COG directamente por HTTP (igual que hace cogeo.org), pero muestra
  el nombre del cliente, el trabajo y la fecha en vez de una interfaz técnica.
- **`cli/publicar_mapeo.py`** — Un script que corrés vos (en tu máquina, no
  en la nube) cada vez que tenés un mapeo nuevo listo. Sube el archivo,
  calcula superficie y resolución, arma el link del visor, y genera un PDF
  con esos datos + un código QR para mandar por WhatsApp.

**Estado actual: v1, solo ortofoto RGB.** El código de NDVI/NDRE/NDWI y DSM
ya está escrito (parámetro `--tipo`), pero no lo probé con archivos reales
porque no tengo esos productos a mano. Cuando tengas uno, probalo con
`--sin-subir` primero y avisame si algo no se ve bien — es la parte menos
madura de todo esto.

## Por qué así (y no integrado a agrogestion.html)

Elegiste que esto vaya separado del sistema de gestión. Ventaja: no toca nada
de lo que ya funciona en Netlify/Railway/Supabase. Desventaja: el mapeo no
queda asociado al cliente en esa base de datos — si más adelante querés que
el bot de Telegram le avise al cliente automáticamente, o que el mapeo
aparezca en la ficha del cliente en agrogestion.html, eso es un paso
posterior, no está acá.

## Instalación (una sola vez)

### 1. Dependencias de Python

```bash
pip install rasterio rio-cogeo fpdf2 pyproj "qrcode[pil]" pillow numpy
```

Todas son paquetes puros de Python (rasterio ya trae GDAL adentro, no hace
falta instalar GDAL aparte).

### 2. GitHub CLI

Instalá `gh` desde https://cli.github.com/ y autenticate una vez:

```bash
gh auth login
```

El script usa esto para subir cada COG como asset de un GitHub Release —
el mismo mecanismo que ya usaste en el ejemplo que me mandaste
(`visentiniemanuelidemsa/MOSAICS`). GitHub sirve esos archivos con soporte
de rangos HTTP, que es justo lo que el visor necesita para no tener que
descargar el archivo entero.

Necesitás un repositorio de GitHub donde ir subiendo los mapeos (puede ser
uno nuevo, dedicado a esto, público o privado — si es privado el cliente
va a necesitar estar logueado en GitHub para ver el archivo, así que para
compartir con productores conviene que sea público).

**Límite a tener en cuenta:** GitHub permite hasta 2 GB por archivo en un
Release. Si tus ortomosaicos de 1 cm/píxel superan eso, vas a necesitar
comprimir más agresivo o buscar otro storage (backblaze B2, Cloudflare R2,
etc. — avisame si llegás a este punto y lo resolvemos).

### 3. Publicar el visor

El visor (`viewer/`) tiene que quedar accesible en una URL pública. Dos
opciones simples:

**Opción A — GitHub Pages (gratis):**
```bash
cd viewer
git init
git add .
git commit -m "Visor de mapas Guyra Agro"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/guyra-mapas.git
git push -u origin main
```
Después, en GitHub → Settings → Pages, activar Pages desde la rama `main`.
Tu visor queda en `https://TU-USUARIO.github.io/guyra-mapas/`.

**Opción B — Netlify** (igual que ya hacés con agrogestion.html): arrastrar
la carpeta `viewer/` a Netlify. Te da una URL tipo
`https://guyra-mapas.netlify.app/`.

### 4. Configurar el script

```bash
cd cli
cp config.example.json config.json
```

Editá `config.json`:
```json
{
  "github_repo": "tu-usuario/guyra-mapas-datos",
  "viewer_base_url": "https://tu-usuario.github.io/guyra-mapas/"
}
```

## Probar todo antes de usar un mapeo real

Incluí `ejemplos/mapa-sintetico-prueba.tif`, un archivo de prueba (no es un
mapeo real, es un degradé de colores) para que puedas correr el script y ver
el visor funcionando antes de gastar tiempo/cuota con tus propios archivos:

```bash
cd cli
python3 publicar_mapeo.py ../ejemplos/mapa-sintetico-prueba.tif \
  --cliente "Cliente de prueba" --trabajo "Prueba" --sin-subir
```

Esto te va a dejar un PDF en `cli/salida/`. Para ver el visor andando de
verdad (con el mapa renderizado, no solo el PDF) hace falta servir el
archivo por HTTP en vez de `file://` — una vez que subas `viewer/` a GitHub
Pages o Netlify (paso 3 más abajo) y tengas un COG real subido a un Release,
el link que te da el script ya funciona directo.

## Uso (cada mapeo nuevo)

```bash
cd cli
python3 publicar_mapeo.py /ruta/al/ortomosaico.tif \
  --cliente "Estancia La Ejemplo" \
  --trabajo "Lote 12" \
  --tipo rgb \
  --fecha 2026-09-12 \
  --nota "Relevamiento post-aplicación"
```

Esto:
1. Verifica que el archivo sea un COG válido (si no, lo convierte).
2. Lo sube a tu repo de GitHub como Release.
3. Calcula superficie (ha) y resolución (cm/píxel).
4. Genera una miniatura y un código QR.
5. Te devuelve el link del visor y un PDF en `cli/salida/`.

Para probar sin subir nada a GitHub todavía (por ejemplo, para revisar cómo
queda el PDF antes de gastar cuota):

```bash
python3 publicar_mapeo.py archivo.tif --cliente "X" --trabajo "Y" --sin-subir
```

## Qué le llega al cliente

Mandale por WhatsApp el link que te devuelve el script, o el PDF (tiene el
mismo link como texto y como QR). El link abre el visor con:
- El mapa ajustado automáticamente a la zona (no hace falta calcular zoom/centro).
- El nombre del cliente y el trabajo arriba.
- Sin menús técnicos ni jerga de GIS.

## Qué falta / próximos pasos posibles

- Probar `--tipo ndvi/ndre/ndwi/dsm` con un archivo real (la rampa de color
  para índices está armada pero sin validar).
- Si en algún momento querés que esto se integre con agrogestion.html
  (que el mapeo quede en la ficha del cliente, que el bot avise por
  Telegram), es una extensión aparte — este paquete no lo asume.
- Si empezás a generar muchos mapeos, puede convenir armar una página
  "índice" que liste todos los trabajos de un cliente en vez de mandar
  links sueltos cada vez.
