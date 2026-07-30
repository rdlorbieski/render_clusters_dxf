# DXF Render API

API HTTP para **extrair e renderizar regiões informativas** de arquivos DXF (AutoCAD) como PNG legível, sem precisar abrir o AutoCAD. Detecta automaticamente carimbos de RT, quadros informativos, legendas e tabelas técnicas — ideal para automação de análise de projetos PSCIP (Prevenção e Combate a Incêndio e Pânico).

---

## O que ela faz

- **Renderiza** qualquer região de um DXF em PNG de alta resolução (DPI adaptativo).
- **Localiza automaticamente** as regiões mais valiosas do desenho usando clustering por densidade de texto + pontuação por palavras-chave PSCIP (CREA, RT, M-3, Saídas de Emergência, Extintores, etc.).
- **Aceita CSV ou strings livres** como entrada, permitindo focar na busca por valores específicos (ex.: dados do projeto, nomes de salas).
- **Classifica a qualidade do DXF** (alta, média, baixa) e ajusta automaticamente os parâmetros de render — funciona em modelspace UTM, mm/paper-space e tudo entre.

---

## Instalação

### Local (desenvolvimento)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt

uvicorn api:app --reload
```

API disponível em `http://localhost:8000/docs` (Swagger UI).

### Docker (produção)

```bash
docker compose up -d --build
```

---

## Configuração

Variáveis de ambiente (arquivo `.env`):

| Variável | Obrigatória | Default | Descrição |
|----------|-------------|---------|-----------|
| `UVICORN_WORKERS` | Não | `1` | Workers do uvicorn |
| `API_PORT` | Não | `8000` | Porta exposta |

---

## Endpoints

| Endpoint | Entrada | Saída | Quando usar |
|----------|---------|-------|-------------|
| `POST /render-region` | DXF + `x`, `y` | PNG | Sabe exatamente onde quer recortar |
| `POST /top-clusters` | DXF | JSON com 5 centros + scores | Descobre regiões valiosas sem palpite |
| `POST /top-clusters-csv` | DXF + CSV | JSON com centros baseados em matches | Tem um CSV com dados a localizar no desenho |
| `POST /render-clusters-csv` | DXF + CSV | ZIP de PNGs | Quer ver tudo do CSV de uma vez |
| `POST /render-clusters-strings` | DXF | ZIP de PNGs | Procura automaticamente por keywords PSCIP (RT, CREA, Extintor, M-3...) |
| `POST /diagnose` | DXF | JSON com layers, extents, side, etc. | Debug — entender o DXF antes de processar |

Todos retornam (em JSON ou no header `X-Qualidade-DWG`) a qualidade detectada do DXF: **alta**, **media** ou **baixa**.

---

## Exemplos rápidos

### 1. Renderizar uma região conhecida

```bash
curl -X POST "http://localhost:8000/render-region?x=358110.53&y=7705639.08" \
     -F "file=@projeto.dxf" \
     --output regiao.png
```

### 2. Descobrir as 5 regiões mais informativas

```bash
curl -X POST "http://localhost:8000/top-clusters?n=5" \
     -F "file=@projeto.dxf"
```

Resposta:
```json
{
  "qualidade_dwg": "alta",
  "qualidade_motivo": "modelspace bem-formado (aspect 2.1, 722 textos)",
  "dominant_layer": "2 - Textos",
  "total_texts": 587,
  "clusters": [
    {
      "x": 360787, "y": 7701611,
      "text_count": 56, "keyword_count": 8, "score": 131.0
    },
    {
      "x": 351431, "y": 7701201,
      "text_count": 37, "keyword_count": 6, "score": 92.0
    }
  ]
}
```

O cluster com **maior `score`** é o mais valioso — combina contagem de textos com bônus de palavras-chave PSCIP.

### 3. Renderizar tudo de um CSV de uma vez

```bash
curl -X POST "http://localhost:8000/render-clusters-csv?n=5" \
     -F "dxf=@projeto.dxf" \
     -F "csv_file=@dados.csv" \
     --output clusters.zip
```

O ZIP vem com `cluster_01_matches9.png`, `cluster_02_matches7.png`, etc.

### 4. Renderizar blocos PSCIP automaticamente

```bash
curl -X POST "http://localhost:8000/render-clusters-strings?n=5" \
     -F "dxf=@projeto.dxf" \
     --output clusters.zip
```

Sem precisar passar palavras-chave — usa a lista interna (RT, CREA, Extintor, Saídas de Emergência, M-3, etc.).

---

## Detecção automática de qualidade

A API classifica cada DXF e ajusta `side` de clustering, margem de render e DPI alvo:

| Qualidade | Critério de detecção | Efeito interno |
|-----------|---------------------|----------------|
| **alta** | modelspace bem-formado (aspect ≤ 5, dim menor ≥ 5000, ≥200 textos) | janelas mais justas, render apertado |
| **media** | qualquer caso intermediário | comportamento padrão |
| **baixa** | paper-space (dim menor < 500) OU aspect ratio > 30 | janelas maiores, mais margem, DPI maior |

Não precisa configurar nada — vem como saída para você saber o que esperar.

---

## Heurísticas de pontuação (scoring)

O endpoint `/top-clusters` pontua cada texto:

- **+1 ponto base** — qualquer texto não-vazio
- **+10 pontos** por keyword PSCIP encontrada (CREA, Responsável Técnico, Saídas de Emergência, Extintor, Iluminação de Emergência, Sinalização de Emergência, Acesso de Viatura, Subestação de Energia, etc.)
- **+10 pontos** se contém `RT` como token isolado
- **+5 pontos** se contém classificação de ocupação (`A-1`, `M-3`, `F-8`, etc.)

O score total do cluster = soma dos scores dos seus membros. A ordenação prioriza clusters semânticos (carimbo + medidas preventivas) sobre clusters puramente densos (tabelas de coordenadas).

---

## Arquitetura

```
api.py                    # FastAPI: endpoints + helpers de scoring e qualidade
dxf_render.py             # Renderização pura: bbox, color policy, render_region
extract_region.py         # Script standalone (CLI) para extrair UMA região
render_text_clusters.py   # Script standalone (CLI) para top clusters
```

O pipeline completo está documentado em [`DOCUMENTACAO.md`](DOCUMENTACAO.md): cada passo (carregamento, análise, detecção de layer, cálculo de side, clusterização, render bounds, left-anchor, DPI adaptativo, render matplotlib) com suas limitações conhecidas.

---

## Limitações conhecidas

- DXFs gerados por softwares não-AutoCAD (Revit, BricsCAD) podem perder entidades proprietárias na leitura
- Aspect ratios extremos (>100:1) podem produzir janelas com espaço em branco
- Textos com 100+ caracteres por linha podem ser cortados à direita (ezdxf não expõe métricas de fonte)
- Performance cai em DXFs com >50k entidades (~25s/cluster vs ~5s para 10k entidades)

Detalhes completos com workarounds em [`DOCUMENTACAO.md`](DOCUMENTACAO.md#6-quando-a-api-não-vai-funcionar-bem).

---

## Stack

- **FastAPI** + **uvicorn** — servidor HTTP
- **ezdxf** — leitura e parsing de DXF
- **matplotlib** (backend Agg) — render PNG
- **Docker** + **docker-compose** — empacotamento

Python ≥ 3.10.

---

## Licença

Uso interno.
