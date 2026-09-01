# Restaurant App Backend

Django + Django REST Framework API for the Angular restaurant-app frontend.

## Setup

```bash
python -m venv venv
venv/Scripts/activate   # source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
```

## Run

```bash
python manage.py runserver
```

API available at `http://localhost:8000/api/`, Django admin at `http://localhost:8000/admin/`.

## Endpoints

- `GET/POST /api/menu-items/` — list (public) / create (auth required)
- `GET/PATCH/DELETE /api/menu-items/<id>/` — read (public) / update, delete (auth required)
- `GET/POST /api/orders/` — list (auth required) / create (public, used by customers placing an order)
- `GET/PATCH/DELETE /api/orders/<id>/` — auth required
- `PATCH /api/order-items/<id>/` — toggle an order item's `completed` flag (auth required)
- `POST /api/auth/login/` — `{ "username", "password" }` -> `{ "token", "username" }`

Authenticated requests must send `Authorization: Token <token>`.

## Default superuser (local dev only)

- username: `admin`
- password: `admin12345`

Change this before deploying anywhere real.
