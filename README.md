# Correção do coletor de lançamentos

## O que esta versão corrige
- Data ausente permanece `null`; nunca vira a data de hoje.
- Status pode ser `upcoming`, `published` ou `unknown`.
- Erros HTTP, timeout e fontes sem registros aparecem em `collection_report.json`.
- Uma execução vazia/falha não sobrescreve um `feed.json` anterior válido.
- URLs relativas e imagens lazy-load (`data-src`, `data-srcset`, `srcset`) são tratadas.
- Deduplicação conservadora por título + autor; não remove subtítulos ou texto entre parênteses.
- ISBN não é obrigatório e não é inventado.
- Feed versionado com campos explícitos e proveniência.
- Testes automatizados para datas, deduplicação e imagens.

## Limitação importante
Este é um coletor defensivo inicial, não uma garantia de que todas as fontes já estejam extraindo livros corretamente. Cada site muda seu HTML; a extração por `schema.org/Book` é a preferida. O fallback de títulos de artigos é mantido com `publication_status: "unknown"` e **não confirma por si só que o título do artigo é um livro**. Para produção, revisar os seletores de cada fonte com amostras reais do HTML e usar feeds RSS/APIs públicas quando disponíveis.

As páginas configuradas em `scraper/collect.py` são pontos de partida e podem precisar ser substituídas por calendários/listas específicas. Verifique as regras de acesso de cada site antes de ativar uma fonte.

## Rodar localmente
```bash
python -m pip install -r requirements.txt
pytest -q
python scraper/collect.py
```

## Arquivos gerados
- `feed.json`: dados para o site.
- `collection_report.json`: resultado por fonte, erros e contagem.

## Recomendações
O cálculo de recomendação 40% autor / 25% gênero / 35% palavras-chave deve ser feito depois de os metadados estarem validados. O coletor não inventa gênero, palavras-chave, sinopse ou editora quando a fonte não os fornece. O CSV do Goodreads deve continuar sendo processado localmente no navegador, como no site atual.


## Clubes de leitura adicionados
Foram incluídos os arquivos oficiais:
- Book of the Month: https://www.bookofthemonth.com/all-months
- Aardvark Book Club: https://www.aardvarkbookclub.com/allbooks

A data da seleção pelo clube não é tratada como data de publicação. Quando a página não fornece uma data de publicação verificável, ela fica como desconhecida. Como o HTML desses sites pode mudar, confira os resultados e o `collection_report.json` antes de confiar na coleta automática.

## Integração com a página
A página tenta carregar `feed.json` (formato de objetos produzido pelo coletor) e continua aceitando `releases.json` antigo. Registros sem data são exibidos como **Data não confirmada**; a data do mês do clube não é convertida em data de publicação. Quando o mesmo título/autor já está no catálogo local, a página mescla fontes e tags em vez de criar um cartão duplicado.

## Validação desta revisão
- `pytest -q`: 8 testes aprovados.
- Sintaxe JavaScript validada com `node --check`.
- A coleta HTTP real não pôde ser executada neste ambiente por falha de resolução DNS; portanto, os seletores dos clubes ainda precisam de confirmação em uma execução no GitHub Actions. O relatório registra falhas por fonte e o coletor preserva um feed anterior em vez de gerar um feed vazio.
