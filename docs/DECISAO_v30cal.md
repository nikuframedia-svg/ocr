# Decisão: variante v30cal + gate de gravação (1 página para o Luís)

**Data:** 2026-07-09 · **Medições:** harness simétrico R253 (controlo
HEAD-vs-HEAD = delta exatamente 0), DB de auditoria humana (sha df08304d,
1147 folhas validadas), plano de 20.839 entries.

## O que se mediu

| Variante | Acerto (TOTAL) | GOOD | Troca de peça | "Fora do plano" detetado |
|---|---|---|---|---|
| **v30 (produção hoje)** | **92,8%** | 110/110 | 111/150 | **0/80** |
| next (R250-R252) | 92,3% | 110/110 | 108/150 | 12/80 |
| **v30cal (proposta)** | **= v30 por construção** | 110/110 | = v30 | **12/80 (15%)** |

O "+0,5pp" que o commit R250-R252 anunciava era artefacto de um harness
viciado (2 bugs, corrigidos e provados). O ranking do v30 é o melhor medido
— **fica como está**.

## O problema real de resultados

**10-30% das linhas reais têm a encomenda fora do plano do dia** (medido,
quant7). Nesses casos o motor substitui a OF correta que o operador
escreveu por uma OF errada do plano — com ar confiante (caso da folha
2367: `esp` correto do operador sobrescrito por valor errado).

O v30cal deteta 15% desses casos (12/80 medidos; o v30 deteta 0) e sabe
dizer a confiança POR CÉLULA calibrada (no bucket de maior confiança do
modelo: diz 97,9% e acerta 91,4% — antes dizia 99,3% e acertava 77,6%).

## As 3 decisões

1. **Soak do v30cal** — ligar `CROSS_SHADOW_VARIANT=v30cal` no .env da
   fábrica. Produção intocada; a sombra corre por folha e a triagem é em
   `/shadow-queue`. Critério de saída formal (SPRT): aborta em ~25 folhas
   se estiver mau; aceita com ≥300 folhas boas. Custo: zero para os
   operadores; reversão = apagar a linha do .env.
2. **Flip do v30cal** (após soak OK) — mesmos valores escritos, cores e
   fila de revisão mais fiáveis. Reversão: `git revert` de 1 commit.
3. **Ligar `CROSS_WRITE_GATE_MARGINAL`** — é isto que converte a deteção
   em linhas finais corretas: célula duvidosa (`very_different`) com
   confiança calibrada abaixo do limiar (esp/comp 98%, identidade 95%,
   98% se houver peça irmã a <2 bits) NÃO sobrescreve o valor do
   operador; fica vermelha para revisão. Sem isto, o valor errado do
   plano continua a ser escrito (regra R219) e só a cor muda.

## Risco e reversibilidade

- v30cal não muda NENHUMA escolha de OF/peça (provado por backtest:
  igualdade exata com o v30 — reports/backtest_winner_v30cal).
- Gate: o pior caso é uma célula certa do plano ficar vermelha à espera
  de revisão (custo: 1 clique); o caso que evita é um valor errado ir
  para o histórico certificável (EN 1090/ISO 9001).
- Monitor pós-flip automático (staleness + CUSUM) alarma se a calibração
  derivar; nunca decide sozinho.
