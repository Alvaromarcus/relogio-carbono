# Relógio de Carbono do SIN

Uma página que responde uma pergunta: **a que horas a energia brasileira é mais limpa — e quanto se ganha deslocando uma carga flexível para lá?**

Eficiência energética sempre foi consumir menos kWh. Com a matriz brasileira atual existe uma segunda dimensão: *quando* se consome vale tanto quanto *quanto* se consome. O mix do Sistema Interligado Nacional muda hora a hora, e deslocar uma carga flexível de uma janela para outra reduz a pegada de carbono da mesma produção — sem investir em equipamento e sem produzir menos.

🔗 **https://alvaromarcus.github.io/relogio-carbono/**

Feito para o slide 13 da palestra *Energia Inteligente*, no 2º Fórum Sinerges.

## Como funciona

Não há servidor, banco de dados nem custo de hospedagem:

```
GitHub Actions (cron 13h e 20h de Brasília)
  └─ scripts/build_sin.py  baixa o parquet do ONS → filtra → calcula
       └─ docs/data/sin.json  commitado automaticamente
            └─ GitHub Pages serve docs/, a página lê o JSON do mesmo domínio
```

Servir o JSON do próprio domínio elimina CORS; o Action commitando no repositório elimina o servidor.

## O que é medido e o que é estimado

| Métrica | Natureza |
|---|---|
| Percentual renovável por hora | **Medição.** Razão direta entre a geração renovável e a geração total publicadas pelo ONS. Não depende de premissa alguma. |
| Intensidade de carbono por hora | **Estimativa.** Depende dos fatores de emissão de ciclo de vida do IPCC AR5, Annex III. |
| Custo marginal de operação (CMO) | **Estimativa.** Calculado pelo modelo DESSEM do ONS. É a base do PLD, não a tarifa paga pelo consumidor. |

O ONS publica a geração térmica como um bloco único — gás, carvão, óleo, biomassa e a nuclear de Angra entram juntos em `val_gertermica`. Sem abertura por combustível, o valor central adotado é o do gás de ciclo combinado (490 kg CO₂eq/MWh) e a página exibe a faixa entre biomassa dedicada (230) e carvão (820) como análise de sensibilidade. A seção "Metodologia e premissas" da própria página traz a tabela completa, com a fonte de cada fator.

## Preço e carbono apontam para a mesma hora?

A página cruza a intensidade de carbono com o [CMO Semi-Horário](https://dados.ons.org.br/dataset/cmo-semi-horario)
do ONS, agregado por média para a hora cheia, e responde se a janela mais barata é também a mais limpa.

A resposta muda por subsistema, e é por isso que a pergunta vale: no Sudeste/Centro-Oeste as duas janelas quase
coincidem, enquanto no Sul e no Nordeste a correlação de postos entre preço e intensidade é fortemente negativa —
lá, as horas mais baratas tendem a ser as mais sujas.

## Rodando local

```bash
pip install -r requirements.txt
python scripts/build_sin.py          # grava docs/data/sin.json
python -m http.server --directory docs
```

Variáveis opcionais: `ANO` (padrão: ano corrente).

## Fontes

- **Dados:** [ONS — Balanço de Energia nos Subsistemas](https://dados.ons.org.br/dataset/balanco-energia-subsistema), base horária, licença Creative Commons Attribution. Os dados passam por processo de consistência recorrente e podem ser revisados depois de publicados.
- **Fatores de emissão:** [IPCC AR5, WG3, Annex III, Table A.III.2](https://www.ipcc.ch/site/assets/uploads/2018/02/ipcc_wg3_ar5_annex-iii.pdf)
- **Validação:** [MCTI/SIRENE — fator médio do SIN](https://www.gov.br/mcti/pt-br/acompanhe-o-mcti/cgcl/paginas/fator-medio-inventarios-corporativos)

## Licença

| O que | Licença |
|---|---|
| Código (`scripts/`, workflow, JS da página) | [MIT](LICENSE) |
| Conteúdo — textos, gráficos e o `sin.json` derivado | [CC BY 4.0](LICENSE-CONTENT.md) |

Uso livre, inclusive comercial, com crédito. Como o dado de origem do ONS é CC-BY, quem
reutilizar o `sin.json` credita o ONS **e** este projeto — as atribuições são cumulativas.

## Como citar

> Relógio de Carbono do SIN, de Álvaro Severo Marcus (CC BY 4.0) —
> https://github.com/Alvaromarcus/relogio-carbono

O arquivo [`CITATION.cff`](CITATION.cff) habilita o botão "Cite this repository" na página
do GitHub, com saída em BibTeX e APA.

## Limitações

A página não faz previsão de intensidade para as próximas horas — mostra apenas o que já foi medido. O balanço do ONS sai com alguns dias de defasagem; a página anuncia o dia de referência com destaque.

---

Álvaro Severo Marcus · [Linkedin](https://www.linkedin.com/in/alvaromarcus/)
