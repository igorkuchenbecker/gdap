# GDAP

Plataforma de automação de dados: conecta em fontes variadas, descobre o que os dados
significam, valida, limpa, transforma, analisa e explica o resultado com evidência — tudo
orquestrado por pipelines declarativos com aprovação humana onde importa.

Não tem regra fixa de negócio embutida: perfila o dataset que recebe e se adapta a partir daí.
Roda numa máquina só com SQLite e Parquet, ou com Postgres, object storage e vários workers —
mesmo código, adaptadores diferentes.

Python 3.13 · FastAPI · Polars/DuckDB · SQLAlchemy · Typer

## Funcionalidades

- **Conectores** — CSV/TSV, JSON, Parquet, XML, Excel; Postgres, MySQL, SQL Server, Oracle,
  SQLite; REST com paginação e retry. Novo conector é plugin, não mexe no core.
- **Ingestão** em chunks (tamanho do dataset limitado por disco, não por RAM), modos full/
  incremental/append, checkpoint e detecção de schema evolution.
- **Perfil automático** de cada coluna: distribuição, cardinalidade, outliers, chaves
  candidatas, relação entre datasets e tipo semântico (moeda, e-mail, identificador...).
- **Qualidade** em 7 dimensões com score 0–100 e expectations declarativas; pode travar um
  pipeline se cair abaixo do limite.
- **Limpeza** proposta com justificativa e nível de aprovação — nada muda silenciosamente.
- **Pipelines** declarativos em YAML com linguagem de expressão própria (sem `eval`, sem
  import, sem acesso a atributo).
- **Análise** descritiva, correlação, segmentação, comparação de período, drivers, tendência,
  forecast com intervalo, 4 métodos de detecção de anomalia.
- **Analista de IA** responde perguntas com evidência anexada (fonte, cálculo, linhas
  consideradas) e funciona sem chave de API nenhuma — ver abaixo.
- **Automação** com retry/backoff, agendamento cron, dependência entre pipelines, alertas.
- **Governança** — RBAC multi-tenant, API keys, trilha de auditoria, lineage, classificação
  automática de sensibilidade, mascaramento, guard de SQL (bloqueia DDL/escrita por padrão).

## Rodando

```sh
uv venv --python 3.13 && uv pip install -e ".[dev]"

gdap system init          # schema + organização padrão
gdap demo run              # gera dados sintéticos e roda o ciclo inteiro, sem chave de API
gdap system serve          # API + web UI em http://127.0.0.1:8000
```

Dados reais:

```sh
gdap source add vendas --connector file.csv --set path=/dados/vendas --set pattern='*.csv'
gdap source ingest vendas --object vendas_2026.csv --dataset vendas
gdap dataset validate vendas
gdap agent ask "por que a receita caiu no último mês?" --dataset vendas
```

CLI, API HTTP e web UI são três clientes da mesma camada de serviço — o que dá pra fazer num
dá pra fazer no outro (`gdap dataset validate vendas --json`, `curl .../api/v1/datasets/...`,
ou a UI em `/`). Schema OpenAPI em `/openapi.json`.

## IA sem chave de API

Provider padrão é determinístico: roteia a pergunta pra ferramenta certa e monta a resposta só
com o que as ferramentas devolveram, sem inventar número. Com `ANTHROPIC_API_KEY` exportada e
`GDAP_AI__PROVIDER=anthropic`, passa a usar Claude de verdade — chave ausente cai de volta pro
determinístico sozinho, sem quebrar nada. Cada agente só chama as ferramentas que recebeu,
toda chamada é auditada, e escrita/SQL destrutivo continuam bloqueados independente do provider.

## Testes

```sh
pytest                    # unit, integração, e2e
ruff check src tests
mypy
gdap doctor                # diagnóstico do runtime
```

Documentação mais a fundo (arquitetura, pipelines, segurança, deploy) em `docs/`; decisões
técnicas registradas em `docs/adr/`.
