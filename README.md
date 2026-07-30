# DXF/DWG Table Extraction API

API HTTP que **localiza e recorta os quadros técnicos de projetos PSCIP** (Prevenção e Combate a Incêndio e Pânico) em arquivos **DXF ou DWG**, entregando cada tabela como um PNG legível — sem abrir o AutoCAD.

Detecta automaticamente quadro-resumo, medidas de segurança, carimbo de RT, memoriais de sinalização, extintores e classificação de ocupação, ancorando a busca em palavras-chave PSCIP e fechando o retângulo de cada tabela pela geometria das linhas do desenho.

---

## O que ela faz

- **Aceita DXF e DWG.** Arquivos `.dwg` são convertidos para DXF automaticamente (ODA File Converter).
- **Encontra as tabelas** ancorando em keywords PSCIP (CREA, RT, Extintor, Saídas de Emergência, Quadro Resumo, M-3…) e fechando o contorno de cada quadro por um grid de ocupação + componentes conexos.
- **Renderiza cada tabela em PNG** de alta resolução (DPI adaptativo à altura do texto), em preto sobre branco — pronto para leitura por pessoas ou por um LLM/OCR.
- **Classifica a qualidade do arquivo** (alta / media / baixa) e é honesta quando o arquivo compromete a extração (ex.: projeto misturado com camada topográfica) — nunca devolve um erro silencioso.

---

## Requisitos

- **Python ≥ 3.10** (imagem oficial usa 3.12).
- **ODA File Converter** — necessário **apenas para entrada DWG**. Em produção já vem instalado na imagem Docker; localmente, instale-o e o `converter.py` o localiza em `C:/Program Files/ODA/...` (Windows) ou `/usr/bin/ODAFileConverter` (Linux). Entradas DXF não precisam dele.

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

Swagger UI em `http://localhost:8000/docs`.

### Docker (produção)

```bash
docker compose up -d --build
```

A imagem inclui o ODA File Converter e o runtime X11/xvfb que ele exige (é um app Qt6, mesmo rodando headless).

---

## Configuração

Variáveis de ambiente (arquivo `.env`, opcional):

| Variável | Default | Descrição |
|----------|---------|-----------|
| `UVICORN_WORKERS` | `1` | Número de workers do uvicorn |

A porta é `8000` (exposta pelo `docker-compose.yml`).

---

## Endpoints

São **três**, todos `multipart/form-data` com o campo `file` (um `.dxf` ou `.dwg`):

| Endpoint | Saída | Quando usar |
|----------|-------|-------------|
| `POST /tables/extract` | **ZIP** (`tabelas.zip`: PNGs + `manifest.json`) | Extrair as tabelas como imagens |
| `POST /tables/info` | **JSON** (qualidade, parâmetros, lista de tabelas) | Inspecionar o que sairia, sem renderizar — rápido |
| `POST /tables/plot` | **PNG** (desenho com os retângulos sobrepostos) | Debug visual: conferir onde os quadros foram detectados |

### Parâmetros de query (comuns aos três)

Referência rápida — os defaults servem para a grande maioria dos projetos, você raramente precisa mexer:

| Parâmetro | Default | Em uma linha |
|-----------|---------|--------------|
| `n` | `5` | Quantas tabelas retornar, no máximo (1–20) |
| `cell_factor` | `1.0` | Resolução do grid de detecção |
| `gap_factor` | `2.5` | O quanto "engorda" as linhas para fechar a tabela |
| `roi_margin_factor` | `60.0` | Tamanho da janela de busca ao redor da keyword |
| `group_factor` | `25.0` | Distância para considerar duas keywords "da mesma tabela" |

> **A régua é a altura do texto.** Os quatro últimos parâmetros são **multiplicadores da altura típica da letra** do desenho (medida no passo 2), não valores absolutos. Por isso os mesmos defaults funcionam num projeto em milímetros e em outro em metros — tudo escala junto.

#### Entendendo cada um

**`n` — quantas tabelas**
Limite de quantas tabelas voltam, já ordenadas pela pontuação de keywords (as mais relevantes primeiro). Só isso: aumente se o projeto tem muitos quadros e você quer todos; diminua para receber só os mais importantes.

**`cell_factor` — resolução do grid**
Para encontrar a tabela, o detector cobre a região com um grid de células e "pinta" cada célula que tem linha ou texto — vira uma espécie de *pixel art* do desenho. `cell_factor` controla o tamanho dessa célula (`célula = altura_do_texto × cell_factor`).
- **Menor** (ex.: `0.5`) → grid mais fino, contorno mais preciso, porém mais lento e mais memória.
- **Maior** (ex.: `2.0`) → grid mais grosso, mais rápido, mas pode borrar detalhes e colar coisas próximas.
- **Mexa se:** tabelas com letra muito miúda estão saindo grudadas → diminua.

**`gap_factor` — fechar os vãos da tabela**
Uma tabela é feita de linhas e textos com espaços entre eles. Para o detector enxergar tudo como **um bloco só** (o "balde de tinta" que conecta o que está próximo), ele engorda um pouco cada célula ocupada, fechando esses vãos internos. `gap_factor` diz o quão grande é esse "engorda" (em alturas de texto).
- **Maior** → fecha vãos maiores; bom para tabelas espaçadas — mas pode **fundir duas tabelas vizinhas** num blob.
- **Menor** → mantém tabelas próximas separadas — mas pode **partir uma tabela** com muito espaço interno em pedaços.
- **Mexa se:** uma tabela sai fatiada → aumente; duas tabelas vizinhas saem coladas → diminua.

**`roi_margin_factor` — a janela de busca**
Depois de achar a palavra-âncora (ex.: `EXTINTOR`), o detector abre uma **moldura ao redor dela** e só procura a tabela dentro dessa moldura. `roi_margin_factor` é o tamanho dessa margem (em alturas de texto) — o default é generoso de propósito, para caber a tabela inteira.
- **Maior** → cabe tabelas grandes ou afastadas da keyword, mas processa uma área maior (mais lento) e pode pegar ruído em volta.
- **Menor** → mais rápido e focado, mas arrisca **cortar** tabelas grandes.
- **Mexa se:** tabelas grandes estão saindo cortadas → aumente.

**`group_factor` — o que é "a mesma tabela"**
Um mesmo quadro costuma ter várias keywords (várias linhas). Antes de tudo, o detector agrupa as keywords que estão **perto umas das outras** (o DBSCAN — pense numa festa: quem está no mesmo canto forma uma roda). `group_factor` é o raio que define "perto" (em alturas de texto). Cada grupo vira uma tabela candidata.
- **Maior** → junta keywords mais distantes no mesmo grupo: **menos tabelas, maiores** — risco de fundir quadros distintos.
- **Menor** → grupos mais apertados: **mais tabelas, menores** — risco de quebrar um quadro em vários.
- **Mexa se:** uma tabela está virando várias → aumente; tabelas distintas viram uma só → diminua.

> **Dica de ajuste:** use o `POST /tables/info` para testar valores **sem renderizar** (é rápido) e o `POST /tables/plot` para **ver os retângulos** sobre o desenho. Só depois chame o `/tables/extract`.

`/tables/plot` também aceita `full` (bool) para plotar a prancha inteira em vez de só as regiões das tabelas.

### Respostas informativas

Quando o arquivo é de qualidade **baixa**, ou nenhuma tabela é detectada, ou nada é renderizável, o endpoint responde **`200`** com um JSON explicando o motivo (`status`, `qualidade_dwg`, `motivo`, `tabelas: []`) — em vez de erro. O `/tables/extract` também expõe no header: `X-Qualidade-DWG`, `X-Tabelas`, `X-Motivo`, `X-Gap-Cells`.

---

## Exemplos rápidos

### 1. Extrair as tabelas (ZIP de PNGs)

```bash
curl -X POST "http://localhost:8000/tables/extract?n=5" \
     -F "file=@projeto.dwg" \
     --output tabelas.zip
```

O ZIP contém `tabela_01_score626_kw20.png`, `tabela_02_...png`, etc., mais um `manifest.json` com score, keywords, bbox e DPI de cada tabela.

### 2. Ver o que sairia, sem renderizar (JSON)

```bash
curl -X POST "http://localhost:8000/tables/info?n=5" \
     -F "file=@projeto.dxf"
```

```json
{
  "qualidade_dwg": "media",
  "motivo": "Arquivo com estrutura adequada: proporção 1.2:1, 2661 textos...",
  "text_height": 10.92,
  "total_tabelas": 5,
  "tabelas": [
    { "index": 1, "score": 626.0, "keyword_count": 20, "text_count": 26,
      "keywords": ["ÁREA TOTAL DO TERRENO", "ÁREA TOTAL CONSTRUÍDA", "..."],
      "bbox": { "x_min": -98040, "y_min": 34855559, "width": 657, "height": 910 } }
  ]
}
```

### 3. Conferir visualmente onde as tabelas foram detectadas (PNG)

```bash
curl -X POST "http://localhost:8000/tables/plot?n=5" \
     -F "file=@projeto.dwg" \
     --output plot.png
```

---

## Como funciona (pipeline)

O `run_pipeline` executa, em ordem:

1. **Avaliar qualidade** — mede o desenho e classifica alta / media / baixa. Baixa aborta com resposta informativa.
2. **Medir a escala do texto** — altura típica da letra de corpo. Esse número é a régua que dimensiona todo o grid do passo 3. É robusto a arquivos que misturam duas escalas de texto (projeto + topografia/cotas).
3. **Detectar tabelas** — textos com keyword PSCIP viram sementes; o DBSCAN agrupa as próximas em vizinhanças; cada vizinhança é rasterizada num grid de ocupação e os componentes conexos dão o retângulo de cada tabela; a região é pontuada pelas keywords internas.
4. **Registrar no log** — qualidade, parâmetros e tabelas detectadas.
5. **Renderizar** (`/tables/extract`) — cada tabela vira um PNG preto-sobre-branco, com DPI calculado para o texto sair legível.
6. **Empacotar** — PNGs + `manifest.json` no ZIP.

---

## Detecção automática de qualidade

| Qualidade | Critério | Efeito |
|-----------|----------|--------|
| **baixa** | paper-space (menor dim < 500), pouquíssimos textos (< 20), ou aspect > 30 | aborta com resposta informativa |
| **media** | caso intermediário — inclui **mistura de escalas** (dispersão de alturas p90/p10 ≥ 20×, típico de camada topográfica) | processa, mas sinaliza para conferência |
| **alta** | modelspace bem-formado (menor dim ≥ 5000, aspect ≤ 5, ≥ 200 textos) e sem mistura de escalas | processa com confiança |

A qualidade sempre volta na resposta (JSON ou header `X-Qualidade-DWG`).

---

## Pontuação de keywords (scoring)

Cada texto é pontuado (`_text_score`) e o score de uma tabela é a soma dos textos dentro do seu retângulo:

- **+1** base — qualquer texto não-vazio
- **+30** por keyword PSCIP (CREA, Responsável Técnico, Extintor, Saídas de Emergência, Quadro Resumo, Medidas de Segurança, Carga de Incêndio, Classificação, Ocupação…)
- **+30** se contém `RT` como token isolado
- **+15** por referência a Norma Técnica (`NT 01/20`, `NT-14/20`…)
- **+10** por classificação de ocupação (`A-1`, `M-3`, `F-8`…)

Uma tabela precisa de **pelo menos 2 keywords** para não ser descartada como falso positivo.

---

## Arquitetura

```
api.py                     # FastAPI: helpers de qualidade, altura de texto e scoring;
                           #          inclui o router de tabelas
converter.py               # DWG → DXF via ODA File Converter
dxf_render.py              # Análise do DXF (analyze_dxf) + render PNG (ezdxf + Matplotlib)
table_pipeline/
  router.py                # Os 3 endpoints /tables/*
  pipeline.py              # run_pipeline (passos 1–4) + render_tables (passo 5)
  geometry.py              # grid de ocupação + componentes conexos → retângulos
  exceptions.py            # exceções de domínio → respostas 200 informativas
batch_extract/             # Script de QA: roda o mesmo pipeline localmente, sem servidor
```

A pasta `docs/` traz diagramas do fluxo (HTML). Alguns scripts CLI de apoio (`extract_region.py`, `render_text_clusters.py`, `analyze_quality.py`, `render_and_check.py`) ficam na raiz.

---

## Limitações conhecidas

- **Arquivos grandes são lentos.** Pranchas com centenas de milhares de entidades (DWG de dezenas de MB) podem levar **>2 min** de ponta a ponta. Se você chama a API por HTTP, use um **read timeout generoso (≥ 180–300 s)** no cliente.
- **Mistura de escalas** (projeto + levantamento topográfico no mesmo arquivo) é detectada e rebaixa a qualidade para `media` — os resultados merecem conferência.
- **DWG exige o ODA File Converter** instalado; sem ele, entradas `.dwg` retornam `422`.
- **Hachuras (HATCH) são ignoradas** no render — são preenchimento que cobre conteúdo e podem travar o desenho; o texto e as linhas das tabelas não dependem delas.
- DXFs de softwares não-AutoCAD (Revit, BricsCAD) podem perder entidades proprietárias na leitura.

---

## Stack

- **FastAPI** + **uvicorn** — servidor HTTP
- **ezdxf** — leitura/parsing de DXF e motor de desenho
- **matplotlib** (backend Agg) — render PNG
- **scikit-learn** (DBSCAN) — agrupamento das sementes de keywords
- **ODA File Converter** — conversão DWG → DXF
- **Docker** + **docker-compose** — empacotamento

Python ≥ 3.10.

---

## Licença

Uso interno.
