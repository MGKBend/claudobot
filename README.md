# OpenClaw — Claude Code piloté depuis Discord

Un salon Discord = un projet = une session Claude Code (dossier `~/projects/<nom>`).
Claude tourne en `bypassPermissions` ; ses questions (`AskUserQuestion`) arrivent dans le salon avec des boutons.

## 1. Créer le bot Discord (à faire une fois)
1. https://discord.com/developers/applications → **New Application** → onglet **Bot** → **Reset Token** (copie-le).
2. Dans **Bot > Privileged Gateway Intents**, active **Message Content Intent**.
3. **OAuth2 > URL Generator** : scopes `bot` + `applications.commands` ; permissions *Manage Channels, View Channels, Send Messages, Read Message History, Attach Files, Add Reactions*. Ouvre l'URL et invite le bot sur ton serveur privé.
4. Active le mode développeur (Discord > Paramètres > Avancés) pour copier l'ID du serveur et ton ID utilisateur.

## 2. Configurer
```bash
cd ~/Desktop/openclaw-setup
cp .env.example .env && chmod 600 .env   # puis remplis les 3 valeurs
```

## 3. Lancer
```bash
uv run python bot.py          # test au premier plan
```
En service (démarre au boot) :
```bash
mkdir -p ~/.config/systemd/user && cp openclaw.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now openclaw
loginctl enable-linger $USER   # peut demander sudo ; garde le bot actif sans session ouverte
journalctl --user -u openclaw -f
```

## Commandes
| Commande | Effet |
|---|---|
| `/project new <nom> [objectif]` | crée `~/projects/<nom>`, `GOALS.md` et un salon |
| `/project link <nom>` | lie le salon courant à un dossier existant |
| `/goal set` / `/goal show` | objectifs du projet (injectés quand ils changent) |
| `/plan <demande>` | Claude planifie ; boutons Approuver / Modifier / Rejeter |
| `/zip [avec_env] [avec_git]` | envoie le projet en .zip dans le salon (sans .env, .git, node_modules, .venv par défaut) |
| `/usage` | limites d'usage 5H et 7D (aussi affichées dans le statut du bot) |
| `/stop`, `/reset`, `/status` | interrompre, nouvelle session, état des projets |

Un message normal dans un salon de projet est envoyé à Claude (⏳ s'il est occupé, le message est mis en file).

## Pièces jointes et fichiers
- Toute pièce jointe (`.env` compris) est téléchargée dans `attachments/` du projet (permissions 600, ajouté au `.gitignore`). Sans texte, elle est transmise à Claude avec ton prochain message ; avec du texte, tout part ensemble.
- Demande « envoie-moi le projet en zip » : Claude utilise ses outils `send_project_zip` / `send_file`. Les `.env` ne sont inclus que si tu le demandes explicitement.
- Limite d'envoi : celle du serveur Discord (10 Mo sans boost) ; au-delà, le bot l'indique.

## Sécurité
Claude a un accès shell complet sur cette machine. Seuls les IDs de `ALLOWED_USER_IDS` sur le serveur `GUILD_ID` sont écoutés (messages, commandes et boutons). Garde le serveur privé et `.env` en `chmod 600`.

## Limites
Les questions en attente lors d'un redémarrage du bot sont perdues (la session Claude, elle, est reprise).
`spike.py` est le test de validation de l'attente longue (`uv run python spike.py 370`).
