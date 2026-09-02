# GDAP

Plataforma de automação de dados: conecta em fontes variadas, descobre o que os
dados significam, valida, limpa, transforma, analisa e explica o resultado com
evidência — tudo orquestrado por pipelines declarativos com aprovação humana onde
importa.

Não tem regra fixa de negócio embutida: perfila o dataset que recebe e se adapta a
partir daí. Roda numa máquina só com SQLite e Parquet, ou com Postgres, object
storage e vários workers — mesmo código, adaptadores diferentes.

Python 3.11+ · FastAPI · Polars/DuckDB · SQLAlchemy · Typer · web UI sem build

## Finalidade

Tirar dados de várias fontes e chegar a uma resposta confiável sem escrever script
descartável toda vez: ingerir, conhecer a estrutura, validar qualidade, limpar com
transparência (nada muda silenciosamente) e gerar análise com evidência anexada —
tudo num ciclo que o negócio consegue acompanhar e auditar.

## Como funciona

- **Conectores** — CSV/TSV, JSON, Parquet, XML, Excel; Postgres, MySQL, SQL Server,
  Oracle, SQLite; REST com paginação e retry. Novo conector é plugin, não mexe no core
- **Ingestão** em chunks (tamanho limitado por disco, não por RAM), modos
  full/incremental/append, checkpoint e detecção de schema evolution
- **Perfil automático** de cada coluna: distribuição, cardinalidade, outliers, chaves
  candidatas, relação entre datasets e tipo semântico (moeda, e-mail, identificador)
- **Qualidade** em 7 dimensões com score 0–100 e expectations declarativas; pode
  travar um pipeline se cair abaixo do limite
- **Limpeza** proposta com justificativa e nível de aprovação
- **Pipelines** declarativos em YAML com linguagem de expressão própria (sem `eval`,
  sem import, sem acesso a atributo)
- **Análise** descritiva, correlação, segmentação, comparação de período, drivers,
  tendência, forecast com intervalo e 4 métodos de detecção de anomalia
- **Analista de IA** responde perguntas com evidência anexada (fonte, cálculo, linhas
  consideradas) e funciona sem chave de API nenhuma — ver abaixo
- **Web UI** para importar um arquivo arrastando, navegar o catálogo, ler o perfil de cada
  coluna e conversar com o analista. Sem build e sem npm: são três arquivos estáticos servidos
  pela própria API, e viajam dentro do pacote
- **Automação** com retry/backoff, agendamento cron, dependência entre pipelines e alertas
- **Governança** — RBAC multi-tenant, API keys, trilha de auditoria, lineage,
  classificação automática de sensibilidade, mascaramento e guard de SQL (bloqueia
  DDL/escrita por padrão). Retenção é **relatada, nunca aplicada sozinha**: versões de dataset
  e arquivos de upload vencidos aparecem em `/api/v1/retention/candidates` e
  `/api/v1/retention/uploads`, e apagar continua sendo decisão humana

## Analista de IA

O provider padrão é determinístico: roteia a pergunta para a ferramenta certa e monta
a resposta só com o que as ferramentas devolveram — não inventa número. Dá para trocar
por um provedor de IA externo via configuração; sem ele, cai de volta para o
determinístico sozinho, sem quebrar nada. Cada agente só chama as ferramentas que
recebeu, toda chamada é auditada e escrita/SQL destrutivo continuam bloqueados
independentemente do provider.

## Como rodar

```sh
uv venv --python 3.13 && uv pip install -e ".[dev]"   # 3.11 e 3.12 também servem

gdap system init          # schema + organização padrão
gdap demo run             # gera dados sintéticos e roda o ciclo inteiro, sem chave de API
gdap system serve         # API + web UI em http://127.0.0.1:8000
```

A UI vem junto: `pip install gdap && gdap system serve` já serve a interface, sem passo de
build. Abrindo `/`, dá para arrastar um CSV para dentro e ter um dataset consultável — o
`POST /api/v1/sources/upload` registra a fonte e ingere numa transação só.

Dados reais pela linha de comando:

```sh
gdap source add vendas --connector file.csv --set path=/dados/vendas --set pattern='*.csv'
gdap source ingest vendas --object vendas_2026.csv --dataset vendas
gdap dataset validate vendas
gdap agent ask "por que a receita caiu no último mês?" --dataset vendas
```

CLI, API HTTP e web UI são três clientes da mesma camada de serviço — o que dá para
fazer num dá para fazer no outro (`gdap dataset validate vendas --json`,
`curl .../api/v1/datasets/...` ou a UI em `/`). Schema OpenAPI em `/openapi.json`.

## Web UI

Três arquivos estáticos servidos pela própria API — sem build, sem npm, sem lockfile (ADR-007).
Ela é cliente da API pública: consome os mesmos endpoints que a CLI e qualquer integração de
terceiro, então nenhum comportamento mora nela. Se uma tela precisa de algo que a API não
responde, a correção é na API.

| Tela | O que dá para fazer |
|---|---|
| Workspace | Ver o estado da plataforma e importar um arquivo arrastando |
| Sources / Datasets | Navegar o catálogo, pré-visualizar, disparar perfil e validação |
| Dataset | Ler o perfil coluna a coluna: distribuição, completude, tipo semântico, correlações |
| Pipelines / Jobs | Rodar, acompanhar e aprovar |
| Reports | Gerar e abrir os relatórios de um dataset |
| AI Analyst | Perguntar em texto e ver a evidência anexada a cada resposta |
| Governance | Catálogo, classificação, auditoria e o que a retenção está reportando |

O painel de perfil desenha o que o profiler mediu, e cada barra vem com o número que ela
representa escrito ao lado: a barra é a comparação, o número é a afirmação. Nada ali depende de
cor para ser lido. Onde o profiler não mediu — texto livre não é contado por valor exato — a
tela diz que não mediu, em vez de dizer que não há repetição.

## Testes

```sh
pytest                    # unit, integração, e2e
ruff check src tests
ruff format --check src tests
mypy
gdap system doctor        # diagnóstico do runtime
```

A CI roda a suíte em Python 3.11, 3.12 e 3.13 — a faixa inteira que o `requires-python`
promete — mais `ruff check`, `ruff format --check` e `mypy` a cada push e pull request.

Outros dois jobs verificam o que um checkout não prova: um instala o wheel num ambiente **sem
árvore de fontes** e pede a UI por HTTP, o outro faz o mesmo contra a imagem Docker. A suíte
rodando do repositório não diz nada sobre o que chega a quem instala o pacote.

Arquitetura, pipelines, segurança e deploy em `docs/`; decisões técnicas em `docs/adr/`.