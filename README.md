# Guyra Agro — Mapas para clientes

Automatiza lo que ya hacías a mano con cogeo.org: tomar un ortomosaico (COG),
subirlo a algún lado y mandarle el link al cliente. Este paquete separa esa
tarea de agrogestion.html/Supabase (tal como pediste): es un sistema aparte,
solo para publicar mapeos.

Trae dos partes:

- **`docs/`** — Un visor propio, con la marca de Guyra Agro, que reemplaza
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
gh auth setup-git
```

El segundo comando es necesario para que `git push` (que el script usa
para publicar cada mapeo) pueda autenticarse solo, sin pedirte usuario y
contraseña cada vez.

El script commitea cada COG directo a la carpeta `docs/mapas/<trabajo>/`
del repo y lo pushea a GitHub. **No se usa GitHub Releases**: los archivos
de un Release se sirven desde otro dominio
(`release-assets.githubusercontent.com`) sin el permiso CORS que un
navegador necesita para leerlos desde una página en otro dominio — esto
causaba que el visor quedara cargando para siempre, sin importar la
velocidad de conexión. Sirviendo el archivo desde `docs/` del mismo repo
que el visor (GitHub Pages), navegador y archivo quedan en el mismo
origen y el problema desaparece — de yapa, GitHub Pages además responde
con permiso CORS abierto.

Necesitás un repositorio de GitHub donde ir subiendo los mapeos (puede ser
uno nuevo, dedicado a esto, público o privado — si es privado el cliente
va a necesitar estar logueado en GitHub para ver el archivo, así que para
compartir con productores conviene que sea público). El visor y los
mapeos van en el **mismo repo** (el visor en `docs/`, los mapeos en
`docs/mapas/`), justamente para que queden en el mismo origen.

**A tener en cuenta:** cada mapeo publicado queda commiteado en el
historial de git del repo, así que el repo va a ir creciendo con el
tiempo (aun comprimidos a JPEG, cada ortomosaico son ~20-40 MB). Si en
algún momento se vuelve un problema de tamaño, se puede mover a un repo
de "datos" separado del repo del visor, o podar mapeos viejos del
historial — avisame si llegás a ese punto.

### 3. Publicar el visor

El visor (`docs/`) tiene que quedar accesible en una URL pública, y tiene
que ser **GitHub Pages sobre este mismo repo** (no Netlify ni otro
servicio): el script publica cada mapeo dentro de `docs/mapas/` de este
repo, así que si el visor viviera en otro lado, visor y archivo quedarían
en dominios distintos y el problema de CORS que ya resolvimos volvería.

```bash
git init
git add .
git commit -m "Visor de mapas Guyra Agro"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/guyra-mapas.git
git push -u origin main
```
Después, en GitHub → Settings → Pages, activar Pages desde la rama `main`,
carpeta `/docs`. Tu visor queda en
`https://TU-USUARIO.github.io/guyra-mapas/`.

### 4. Configurar el script

```bash
cd cli
cp config.example.json config.json
```

Editá `config.json`:
```json
{
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
archivo por HTTP en vez de `file://` — una vez que publiques `docs/` en
GitHub Pages (paso 3 más abajo) y corras el script sin `--sin-subir`, el
link que te da ya funciona directo.

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
2. Lo commitea a `docs/mapas/` de tu repo de GitHub y lo pushea (mismo
   origen que el visor, para evitar el bloqueo de CORS de los Releases).
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
