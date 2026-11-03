# cazati-nf-api (Flecha)

Recebe o XML autorizado da NF-e (Sysemp), gera o DANFE em PDF, guarda PDF + XML no Supabase e devolve links assinados de 14 dias.

## Rotas
| Rota | Uso |
|---|---|
| `GET /health` | Teste de vida |
| `POST /nf` | Corpo JSON `{"xml_base64": "<campo arquivo da Sysemp>"}` · header `X-Api-Key` |
| `POST /limpeza` | Apaga arquivos com mais de 14 dias · header `X-Api-Key` |

Resposta do `/nf`: `dados` (número, chave, pedido, CPF, nome, e-mail, fone, transportadora, valor), `link_pdf`, `link_xml`, `pdf_base64`, `arquivo_pdf`, `arquivo_xml`.

## Variáveis de ambiente
`API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET` (notas-fiscais), `RETENCAO_DIAS` (14). `DRY_RUN=1` testa sem Supabase.
