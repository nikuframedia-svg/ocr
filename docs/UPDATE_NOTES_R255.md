# Notas de atualização — release R247→R255 (2026-07-09)

Este update leva a fábrica de rev01a (37063e8) até ao R255. São ~21 commits:
as rondas de motor R247–R249, a variante `next` R250–R252 (desligada), o
Task C (/admin, unidades, KPIs) e o R253–R255 (harness simétrico,
calibração da confiança, variante v30cal).

## Como atualizar (no PC da Metalogalva)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\update.ps1
```

## Verificação pós-update

1. Abrir `/health`: `git_sha` tem de ser o HEAD novo e `engine_version`
   tem de dizer **v30_R249**.
2. Abrir uma folha antiga em `/sheet/<id>`: a primeira abertura re-corre o
   cross-check (regeneração on-demand por causa do bump v30_R249 — é o
   mecanismo normal de qualquer ronda de motor; demora ~1-2 s por folha,
   só na primeira vista).
3. Processar 1 folha nova ponta-a-ponta e confirmar cores/valores normais.

## O que NÃO muda (por construção)

- **As decisões do motor são as do v30** — a variante default é "v30", o
  gate de gravação continua OFF, a sombra continua "current". Nenhuma
  flag de .env precisa de mudar.
- Os dados runtime (app.db, refs, lexicons aprendidos) ficam intactos
  (proteções do update.ps1).

## O que muda (visível)

- Rondas R247–R249 no motor de produção: o código-peça embebido decide o
  modelo entre irmãs; irmãs ambíguas ficam vermelhas (validado por
  backtest na altura: GOOD 110/110).
- Task C: /admin com separadores (Referências | Kanbans | Unidades |
  KPIs). Se se quiser proteger, definir `ADMIN_TOKEN` no .env (opcional).
- Novas páginas de suporte ao soak: `/shadow-queue` e
  `/sheet/<id>/shadow-view` (vazias até haver sombra ligada).
- Telemetria de confiança calibrada nos JSONs de cross-check (p_of, p_h0,
  p_field) — não decide nada em v30.
- Monitor de calibração no ciclo de aprendizagem (a cada 50 folhas
  validadas): staleness dos parâmetros + CUSUM; só emite alarmes, nunca
  altera o motor.

## Próximos passos (decisão do Luís — ver docs/DECISAO_v30cal.md)

1. **Iniciar o soak da v30cal**: acrescentar ao `.env`
   `CROSS_SHADOW_VARIANT=v30cal` e reiniciar (ou correr o update.ps1 de
   novo). A sombra corre por folha sem tocar em produção; acompanhar em
   `/shadow-queue` e com `uv run python scripts/diag/soak_sprt.py --db
   data/app.db`.
2. Flip da v30cal após soak OK (commit isolado, reversível).
3. Ligar `CROSS_WRITE_GATE_MARGINAL` — o passo que converte a deteção de
   "fora do plano" em linhas finais corretas.

## Reversão

`git revert` do(s) commit(s) em causa ou, para tudo: contactar o dev — o
update.ps1 nunca apaga dados locais.
