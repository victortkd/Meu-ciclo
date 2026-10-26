# Meu Ciclo 🌸

## Rodar localmente
    pip install -r requirements.txt
    python app.py        # http://127.0.0.1:5000

## Hospedar (recomendado: Render, com disco persistente)
1. Suba esta pasta para um repositório no GitHub.
2. No Render: New > Blueprint > selecione o repositório (usa o `render.yaml`).
   O `SECRET_KEY` é gerado sozinho e o disco de 1 GB guarda o `ciclo.db`.
3. Pronto: o app fica em https://SEU-APP.onrender.com

Outras opções: Railway/Fly.io (use o `Dockerfile` e monte um volume em `/data`)
ou qualquer host com `Procfile` (defina as variáveis do `.env.example`).
IMPORTANTE: sem disco/volume persistente o SQLite é apagado a cada deploy.
Mantenha 1 worker (já configurado), pois o SQLite é um arquivo local.

## Login com Google
1. console.cloud.google.com > Credenciais > ID do cliente OAuth (Aplicativo da Web).
2. URI de redirecionamento: https://SEU-APP.onrender.com/auth/google/callback
   (local: http://127.0.0.1:5000/auth/google/callback)
3. Preencha GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET nas variáveis do host.
Sem isso, o botão do Google fica desativado e o email/senha continua funcionando.
