# BD Personnalisée — EnfantProdige

Application de personnalisation de BD avec le prénom de l'enfant.

## Déploiement sur Fly.io

```bash
fly launch --no-deploy
fly secrets set SUPABASE_URL=... SUPABASE_ANON_KEY=...
fly deploy
```
