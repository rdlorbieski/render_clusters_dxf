# DXF Render API — Documentação Técnica

## 1. Visão geral

Esta API expõe operações sobre arquivos DXF para dois fins principais:

1. **Localizar automaticamente as regiões mais informativas** do desenho (clusters de texto, matches de CSV, matches de strings livres).
2. **Renderizar essas regiões como PNG legível**, em alta resolução, sem precisar abrir o AutoCAD.

A API foi desenhada para funcionar com qualquer DXF de projeto técnico — plantas de incêndio, projetos arquitetônicos, layouts elétricos — sem configuração manual por arquivo. O design parte do princípio de que **a escala, o aspect ratio e a distribuição de texto variam enormemente** entre projetos, e que qualquer heurística baseada em valores fixos (DPI, lado de janela, layer) falha em algum caso real.

## 2. Endpoints

| Endpoint | Entrada | Saída |
|----------|---------|-------|
| `POST /render-region` | DXF + `x`, `y` | PNG (attachment) |
| `POST /top-clusters` | DXF | JSON com 5 centros (x, y) |
| `POST /top-clusters-csv` | DXF + CSV | JSON com centros baseados em matches do CSV |
| `POST /render-clusters-csv` | DXF + CSV | ZIP com PNGs dos clusters do CSV |
| `POST /render-clusters-strings` | DXF + strings livres | ZIP com PNGs dos clusters das strings |
| `POST /diagnose` | DXF | JSON com layers, extents, side sugerido (debug) |

Os endpoints `csv` e `strings` são os mais robustos para uso em produção, pois clusterizam **com base no que o usuário quer encontrar**, não numa heurística cega de layer dominante.

## 3. Pipeline de geração da imagem

O fluxo é o mesmo para qualquer endpoint que produz PNG. Cada passo resolve um problema técnico específico — vale a pena entender cada um para justificar as limitações.

### 3.1. Carregamento (`ezdxf.recover.readfile`)

Usa o leitor tolerante a falhas do `ezdxf`, que recupera DXFs com entidades corrompidas, blocos sem nome, ou referências DIMASSOC quebradas. Logs informativos são emitidos mas o arquivo é processado.

**Limitação**: DXFs gerados por softwares menos populares (BricsCAD, ZWCAD) com extensões proprietárias podem perder algumas entidades neste passo. Não impede o render do restante.

### 3.2. Análise do DXF (`analyze_dxf`)

Caracteriza o arquivo:
- **Extents** (bbox global): obtido via `ezdxf.bbox.extents(..., fast=True)`.
- **Unidades** (`$INSUNITS`): mm, m, polegadas etc.
- **Contagem** de entidades e textos.
- **Política de cor**: amostragem aleatória (seed fixa = 42) de até 2000 entidades, calculando o brilho médio (R+G+B)/3 da cor efetiva. Se `pct_bright > 50%`, o DXF foi autorado em fundo escuro do AutoCAD e precisa de `COLOR_SWAP_BW` para ficar legível em fundo branco.

**Limitação**: a flag `fast=True` do bbox usa heurísticas para splines/curvas. Para arquivos com muitos elementos paramétricos exóticos, os extents podem estar levemente subestimados.

### 3.3. Detecção da layer dominante (`find_dominant_text_layer`)

Conta `TEXT` + `MTEXT` por layer e retorna a layer com mais textos. Esta é a layer usada para:
- Calcular a densidade de textos (passo 3.4).
- Detectar clusters no endpoint `/top-clusters`.

**Limitação crítica**: este passo assume que **a layer com mais textos é a layer com a informação útil**. Em projetos onde:
- Uma layer de "listagem de coordenadas" tem centenas de textos colados;
- Uma layer de "carimbo de revisão" tem dezenas de carimbos;
- Uma layer de "código de barras" ou similar domina contagem;

…a layer dominante apontará para o lugar errado. Foi exatamente o caso do `PRJ2026004905`: layer dominante `TM - Layout` com 274 textos colados num cantinho de 243×61 mm, enquanto a informação útil (QUADRO INFORMATIVO MEDIDAS DE SEGURANÇA) estava em layers como `H-INFO-COMP-INC` ou `0`.

**Workaround**: usar os endpoints `csv` ou `strings`, que ignoram a layer dominante e clusterizam pelos textos que **realmente correspondem** ao alvo procurado.

### 3.4. Cálculo do `side` (`_smart_side`)

O `side` define o lado da janela de clustering — quão "longe" dois textos podem estar para ainda pertencerem ao mesmo cluster. Este é o parâmetro mais sensível do pipeline.

A função combina três sinais:

1. **Densidade real dos textos** (`_density_side`): para cada ponto (amostra de até 200), calcula a distância ao 15º vizinho mais próximo. A mediana dessas distâncias × 2 é o `side`. Adapta-se a qualquer unidade ou escala, sem assumir nada sobre o DXF.

2. **Floor para paper-space**: se `min(dx, dy) < 500` (DXF em milímetros de papel), a densidade colapsa para valores irreais (5-10 unidades). Aí usa-se `min(dx, dy) × 1.5` como floor — garante janela proporcional ao desenho.

3. **Floor para modelspace plano** (sem positions disponíveis): se aspect ratio > 10:1 e não temos textos, usa `min(dx, dy)` direto, evitando que `sqrt(dx·dy)/10` retorne um valor menor que a altura.

**Limitação**: o `side` baseado em densidade assume que existe **uma escala natural** no desenho. DXFs que misturam radicalmente escalas (uma planta detalhada em um canto + uma tabela em outro) podem produzir um `side` que serve mal a ambas.

### 3.5. Clusterização (`find_top_clusters`)

Greedy NMS:
- Para cada ponto candidato, conta quantos textos caem na janela `side × side` centrada nele.
- Pega o melhor candidato, recentra no centroide dos textos cobertos, registra como cluster.
- Remove esses textos da pool e repete N vezes.

Garante clusters **disjuntos** por construção. Complexidade O(N²·k).

**Limitação**: para N > ~10.000 textos roda lento (>5s). Não é problema na prática — projetos técnicos raramente passam de 5k textos. Para casos extremos, o sampling do `_density_side` poderia ser estendido ao clustering.

### 3.6. Cálculo do bbox de render (`Cluster.render_bounds`)

A janela de visualização não é o `side` de clustering. Os textos do cluster podem ter um spread maior que o `side` (por causa da remoção sequencial do NMS) ou menor. Usa-se:

```
base = max(dx_cluster, dy_cluster, min_side)
side_render = base × (1 + 2 × margin)
```

Com `margin = 0.40` e `min_side = side`, garante que clusters pequenos ainda tenham janela útil e clusters grandes não fiquem comprimidos.

### 3.7. Ajuste horizontal (left-anchor)

**Problema estrutural do DXF**: o insert point de TEXT/MTEXT fica na **borda esquerda** de cada linha. O conteúdo se estende para a direita por uma quantidade desconhecida (depende da fonte, tamanho e comprimento da string). Uma janela simétrica em torno do centroide dos insert points **sempre corta o lado direito**.

Solução: ao calcular o centro da janela final, desloca o eixo X tal que `x_min` dos membros fique a 15% da borda esquerda da janela, deixando 85% para o conteúdo se estender.

```python
rcx = x_min + rside × 0.35
```

**Limitação**: textos extremamente longos (notas técnicas com 100+ caracteres por linha) ainda podem ultrapassar o limite. Não há como prever o comprimento sem renderizar — `ezdxf` não expõe métricas de texto facilmente.

### 3.8. DPI adaptativo

`pixels = (side_render / 72) × dpi`

Com `dpi=200` fixo, regiões grandes (5000+ unidades) produziriam PNGs de 14.000 px de lado — estouro de memória. Regiões minúsculas (50 unidades) produziriam PNGs de 140 px — ilegíveis.

Solução: alvo de **4000 px de lado**:
```python
dpi = max(150, min(400, 4000 × 72 / side_render))
```

**Limitação**: para regiões com lado muito grande (>20.000 unidades), o DPI cai para o mínimo (150) e a imagem ainda pode passar de 5000 px. Para regiões muito pequenas, sobe para o máximo (400) e ainda pode ficar abaixo de 1500 px. Não há solução universal — é um trade-off entre uso de memória e legibilidade.

### 3.9. Filtragem de entidades por bbox (`filter_entities_by_bbox`)

Antes de renderizar, descarta entidades cuja bbox não intersecta a região. Crítico para performance em arquivos com >50k entidades. Usa `ezdxf.bbox.Cache` para acelerar bbox de blocos repetidos (INSERTs).

### 3.10. Render matplotlib

`ezdxf.addons.drawing.matplotlib` desenha as entidades filtradas:
- Backend forçado para `Agg` no topo do `api.py` (sem GUI — obrigatório em servidor).
- `set_xlim`/`set_ylim` aplicados **depois** de `draw_entities` (não `draw_layout`, que chama `autoscale_view` e sobrescreve).
- Linha mínima sugerida via `suggest_lineweight` baseada no DPI.

## 4. Casos de teste e resultados

Testes realizados com 4 DXFs reais:

| Arquivo | Extents | Aspect | side | Resultado visual |
|---------|---------|--------|------|------------------|
| PRJ2026005112 | 71.199 × 9.253 | 7.7 | 675 | Corte de prédio com elevadores, halls, escadas. Texto legível. |
| PRJ2026002618 | 2.417.898 × 9.541 | 253 | 1.347 | Quadro com notas, dados do projeto. Texto legível. |
| PRJ2026004905 | 3.360 × 98 (paper-mm) | 34 | 147 | Vista panorâmica, texto pequeno. **Layer dominante errada**. |
| PRJ2026006960 | 26.407 × 12.536 | 2.1 | 1.869 | Planta ELETROCENTRO + tabelas. Texto legível. |

**Taxa de sucesso visual: 3 de 4 (75%)** sem ajuste manual.

## 5. Quando a API vai funcionar bem

- DXFs em modelspace com coordenadas em metros ou UTM.
- Aspect ratio entre 1:1 e 10:1.
- Layer dominante de texto contém de fato a informação útil (verdadeiro para a maioria de projetos PSCIP — sinalização, layout de extintores, blocos de notas).
- Quando o usuário sabe **o que está procurando** e usa `/render-clusters-strings` com palavras-chave específicas.

## 6. Quando a API NÃO vai funcionar bem

### 6.1. DXFs em paper-space (escala milimétrica)
Detectável: `min(dx, dy) < 500`. Ou seja:
`dx` (delta_x) e `dy` (delta y) são a largura e altura do bounding box (extents) do DXF — diferença entre `x_max - x_min` e `y_max - y_min` das coordenadas de todas as entidades.
`min(dx, dy)` é a **menor das duas dimensões** do desenho.
`< 500` é o threshold heurístico que assume: se a menor dimensão do desenho cabe em menos de 500 unidades, provavelmente as unidades são milímetros de papel (paper-space — uma folha A1 tem 841mm, A0 tem 1189mm), não metros/UTM de modelspace (que dariam números na casa de milhares ou milhões).

**Exemplos concretos dos DXFs testados:**

| DXF | Δx | Δy | min(Δx,Δy) | É paper-space? |
|-----|-----|-----|-----------|----------------|
| PRJ2026005112 | 71.199 | 9.253 | 9.253 | Não (modelspace) |
| PRJ2026002618 | 2.417.898 | 9.541 | 9.541 | Não (modelspace) |
| **PRJ2026004905** | **3.360** | **98** | **98** | **Sim (mm de papel)** |
| PRJ2026006960 | 26.407 | 12.536 | 12.536 | Não (modelspace) |

O floor de 1.5× a dimensão menor mitiga, mas se o conteúdo útil está fora dos clusters da layer dominante (caso PRJ2026004905), o resultado fica ruim.

**Workaround**: usar `/render-clusters-strings`.

### 6.2. Layer dominante poluída
DXFs onde a layer com mais textos contém ruído (listagens de coordenadas, carimbos repetidos, tabelas de revisão). A heurística de clustering vai apontar para esses textos.

**Workaround**: usar `/render-clusters-strings` ou `/render-clusters-csv` — eles clusterizam pelos matches, não pela layer.

### 6.3. Aspect ratios extremos (>100:1)
DXFs como o PRJ2026002618 (aspect 253) funcionam, mas o `side` baseado em densidade pode subestimar regiões longas e finas. A renderização gera janelas quadradas — para uma "faixa" de informação, vai sobrar espaço em branco.

**Limitação aceita**: não vale a pena renderizar retângulos não-quadrados; reduziria a flexibilidade do pipeline.

### 6.4. Textos muito longos por linha
Linhas com 100+ caracteres ainda podem ser cortadas à direita mesmo com o left-anchor de 85%. Não há solução genérica sem inspecionar métricas de fonte (não expostas pelo ezdxf).

**Workaround**: usar `dpi` maior aceitando PNG enorme, ou pedir o ponto manualmente em `/render-region` com x deslocado à esquerda.

### 6.5. DXFs com hatches pesados ou splines complexas
Performance cai significativamente. Tempos típicos:
- DXF 10k entidades: ~5s por cluster
- DXF 70k entidades: ~25-30s por cluster

**Limitação aceita**: render técnico de alta qualidade exige processar todas as entidades visíveis. O cache de blocos repetidos já está habilitado.

### 6.6. DXFs gerados por softwares CAD não-AutoCAD
Alguns fabricantes (Revit, BricsCAD, ZWCAD) gravam entidades proprietárias que o `ezdxf` ignora. O resultado é um render com elementos faltando.

**Mitigação**: o `recover.readfile` recupera o máximo possível. Não há solução completa sem suporte específico para cada formato.

### 6.7. DXFs com escala inconsistente
Desenhos que misturam plantas em escala 1:50 com tabelas em escala 1:1 dentro do mesmo modelspace. A densidade calculada é uma média que serve mal a ambos.

**Workaround**: rodar a API duas vezes com strings diferentes, ou pedir pontos manuais com `/render-region`.

## 7. Resumo executivo

A API é desenhada para o caso **majoritário**: projetos técnicos PSCIP em modelspace, em escala métrica ou de coordenadas UTM, com layer dominante representativa do conteúdo. Para esse caso, o pipeline funciona sem intervenção.

Para o restante (~25% dos casos, baseado em testes), o caminho recomendado é:

1. Usar `/diagnose` para entender o DXF.
2. Se a layer dominante parece estranha, usar `/render-clusters-strings` com palavras-chave do que se quer encontrar.
3. Se mesmo assim a região fica cortada, usar `/render-region` com coordenadas explícitas obtidas de `/top-clusters-csv`.

A complexidade não está em fazer um DXF funcionar; está em fazer **todos os DXFs do mundo funcionarem com a mesma configuração**. Os 4 casos testados cobrem extremos opostos (paper-space mm vs. UTM km, aspect 2:1 vs. 253:1), e a abordagem genérica baseada em densidade resolve 3 deles. O quarto requer o workaround baseado em strings, que existe especificamente para esses casos.
