# Sync Party

Watch party synchronisée multi-plateforme. YouTube, Spotify, Deezer, SoundCloud, fichiers locaux.

## Structure

```
sync-party/
├── server.py            # FastAPI app
├── providers/           # Plateformes de diffusion
│   ├── __init__.py
│   ├── base.py          # Provider interface
│   ├── youtube.py       # YouTube (iframe API)
│   └── spotify.py       # Spotify (Web API)
├── templates/
│   ├── index.html       # Landing / création room
│   ├── admin_login.html # Auth admin
│   ├── admin.html       # Dashboard admin
│   ├── watch.html       # Viewer (multi-mode)
│   └── shared/          # Composants partagés
├── static/              # Assets statiques
├── requirements.txt
├── render.yaml          # Déploiement Render
└── README.md
```

## Rôles

| Rôle | Permissions |
|---|---|
| **Admin** | Tout : playlist, mode, mute, kick, promote, source |
| **Moderator** | Changer playlist, mode, source |
| **Viewer** | Regarder, switcher son propre mode |
| **DJ** | Viewer promu — peut skip/prev |

## Sources supportées

| Source | Statut | Auth |
|---|---|---|
| YouTube | ✅ | OAuth 2.0 |
| Spotify | 🔧 En cours | OAuth 2.0 |
| Deezer | 📋 Prévu | Cookie-based |
| Fichiers locaux | 📋 Prévu | Chemin dossier |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload
```
