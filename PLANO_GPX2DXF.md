# Aplicativo GPX → DXF Georeferenciado (Garmin)

Converter arquivos `.gpx` gerados por dispositivos Garmin em arquivos `.dxf` georeferenciados com **blocos AutoCAD**, prontos para abrir em editores CAD.

---

## Decisões de Design

- **Formato DXF** — Formato aberto, compatível com todos os CADs. Gerado via Python.
- **Coordenadas UTM** — Conversão automática de WGS84 (lat/lon) → UTM. Fuso detectado automaticamente.
- **Blocos AutoCAD** — Cada símbolo Garmin será mapeado para um bloco definido diretamente no DXF.

---

## Mapeamento Símbolos Garmin → Blocos AutoCAD

| Símbolo Garmin | Bloco DXF | Layer | Cor AutoCAD | Descrição |
|---|---|---|---|---|
| `Flag, Blue` | `POE` | `POSTE` | 5 (azul) | Poste existente |
| `Flag, Red` | `PR` | `POSTE` | 1 (vermelho) | Poste retirar |
| `Flag, Green` | `PI` | `POSTE` | 3 (verde) | Poste implantar |
| `Flag, Yellow` | `PS` | `POSTE` | 2 (amarelo) | Poste substituir |

---

## Tecnologia: Python

| Biblioteca | Função |
|---|---|
| `gpxpy` | Parser de arquivos GPX |
| `ezdxf` | Geração de arquivos DXF com blocos |
| `pyproj` | Conversão WGS84 → UTM |

---

## Arquivos do Projeto

### `gpx2dxf.py` (script principal)

**Funções:**

1. **`detect_utm_zone(lat, lon)`** — Calcula o fuso UTM e retorna o código EPSG
2. **`convert_coords(lat, lon, epsg_code)`** — Converte WGS84 → UTM via pyproj
3. **`create_block_definitions(doc)`** — Cria os 4 blocos no DXF:
   - **`POE`** — Poste existente
   - **`PR`** — Poste retirar
   - **`PI`** — Poste implantar
   - **`PS`** — Poste substituir
4. **`get_block_name(symbol)`** — Mapeia símbolo Garmin → nome do bloco
5. **`parse_gpx(filepath)`** — Lê waypoints do GPX
6. **`create_dxf(waypoints, output_path)`** — Gera o DXF com:
   - Definições de blocos POE, PR, PI, PS
   - `INSERT` de cada bloco na posição UTM do waypoint
   - `TEXT` com nome do waypoint ao lado de cada ponto
   - Layer "POSTE"
   - Metadados (GPX original, fuso UTM, data)
7. **`main()`** — CLI: `python gpx2dxf.py "arquivo.gpx"`

### `requirements.txt`

```
gpxpy
ezdxf
pyproj
```

---

## Como Usar

```bash
cd "c:\Dev\Script Garmin GPX"
pip install -r requirements.txt
python gpx2dxf.py "garmin waypoints.gpx"
```

Resultado: arquivo `garmin waypoints.dxf` georeferenciado em UTM, com blocos POE/PR/PI/PS conforme os símbolos do GPX.

---

## Verificação

- 4 blocos suportados (POE, PR, PI, PS)
- Inserções de bloco compatíveis com os símbolos presentes no GPX de entrada
- Coordenadas UTM (~333.000 E, ~7.394.000 N para região de São Paulo)
- Abrir no CAD e confirmar que os blocos aparecem nas posições corretas
