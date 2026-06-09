# GeoTools

Aplicativo para converter waypoints `.gpx` (Garmin) e arquivos `.kml` em um `.dxf` georreferenciado em UTM, com blocos AutoCAD (`POE`, `PR`, `PI`, `PS`) e rótulos de texto.

## Instalação (Windows — sem Python)

Execute o comando abaixo no **PowerShell** para baixar e instalar automaticamente:

```powershell
irm https://raw.githubusercontent.com/edmarmaia/GeoTools/main/install.ps1 | iex
```

O instalador:
- Baixa o executável mais recente do GitHub
- Instala em `%LOCALAPPDATA%\GeoTools\`
- Adiciona ao PATH do usuário (permanente)

Após a instalação, abra um novo terminal e execute:

```powershell
GeoTools
```

Para desinstalar, basta apagar a pasta `%LOCALAPPDATA%\GeoTools\` e remover o caminho do PATH.

---

## Arquivos

- `gpx2dxf.py`: script principal
- `Blocos/`: biblioteca de blocos DXF reais (`POE.dxf`, `PR.dxf`, `PI.dxf`, `PS.dxf`)
- `requirements.txt`: dependências sugeridas
- `garmin waypoints.gpx`: exemplo de entrada

## Requisitos

1. Instalar Python 3.11+ no Windows
2. Opcionalmente instalar dependências:

```powershell
py -m pip install -r requirements.txt
```

O script funciona sem `gpxpy` e sem `ezdxf`. Se `pyproj` estiver instalado, a conversão UTM usa a biblioteca; caso contrário, usa o fallback matemático interno.

## Uso

```powershell
py gpx2dxf.py "garmin waypoints.gpx"
```

Ou abrir o menu interativo no terminal:

```powershell
py gpx2dxf.py
```

Tambem pode forcar o menu interativo:

```powershell
py gpx2dxf.py --interactive
```

Ou definindo saída:

```powershell
py gpx2dxf.py "garmin waypoints.gpx" -o "saida\\waypoints.dxf"
```

Ou apontando explicitamente a pasta dos blocos:

```powershell
py gpx2dxf.py "garmin waypoints.gpx" --blocks-dir ".\\Blocos"
```

Ou adicionando vias e nomes de vias do OpenStreetMap:

```powershell
py gpx2dxf.py "garmin waypoints.gpx" --blocks-dir ".\\Blocos" --with-osm
```

Se a API Overpass estiver lenta, reduza a área consultada:

```powershell
py gpx2dxf.py "garmin waypoints.gpx" --blocks-dir ".\\Blocos" --with-osm --osm-margin-m 120
```

Consultas OSM maiores agora sao divididas automaticamente em blocos menores para reduzir timeouts e erros `504 Gateway Timeout` em areas urbanas densas.
Quando o GPX tiver pontos em areas distantes, o script separa os agrupamentos antes de consultar o Overpass para evitar um recorte unico cobrindo grandes vazios.

## Mapeamento

- `Flag, Blue` -> `POE`
- `Flag, Red` -> `PR`
- `Flag, Green` -> `PI`
- `Flag, Yellow` -> `PS`
- sem `sym` ou com `sym` nao mapeado -> `WPT_GENERICO` (circulo vermelho)

## Observações

- O DXF é gerado em coordenadas UTM reais, pronto para abrir em CAD.
- No modo interativo, o terminal mostra um menu com o nome do aplicativo e as opções de seleção.
- A seleção do arquivo `.gpx` e da pasta `Blocos` abre o Explorer do Windows para o usuário escolher.
- No modo interativo, a margem OSM também pode ser alterada diretamente pelo menu do terminal.
- Quando a pasta `Blocos` estiver disponível, o script embute os DXFs reais dos blocos no arquivo final.
- Se algum bloco externo não existir, o script usa um fallback geométrico simples.
- Cada ponto recebe nome e coordenada UTM formatada ao lado, por exemplo `(0333288,7394610)`.
- Cada ponto também recebe um círculo vermelho de destaque na layer `POSTE_DESTAQUE`, ajustado ao conjunto ponto + textos.
- Com `--with-osm`, o script busca vias do OpenStreetMap via Overpass API.
- As vias entram na layer `VIA` e, quando houver `name`, os nomes na layer `VIA_NOME`.
- Cada via é desenhada com duas linhas paralelas, simulando as bordas da rua.
- O recorte OSM usa uma margem configurável por `--osm-margin-m`.
- Recortes OSM maiores sao subdivididos automaticamente antes de consultar o Overpass.
- Waypoints distantes passam a ser consultados em grupos separados, evitando um bbox unico muito grande.
- Se um bloco Overpass falhar por timeout ou erro HTTP, o script subdivide esse bloco novamente antes de desistir.
- O script tenta mais de uma instância Overpass automaticamente quando houver timeout ou erro HTTP.
- A zona UTM é detectada automaticamente a partir do primeiro waypoint.
- Todos os waypoints do mesmo arquivo são exportados no mesmo sistema UTM.
- O mapeamento segue o `PLANO_GPX2DXF.md`, mesmo que alguns arquivos LSP antigos usem siglas com significado diferente.
