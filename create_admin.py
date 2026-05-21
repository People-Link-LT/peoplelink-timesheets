"""
Run once to create the first admin account:
  python create_admin.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from app.database import init_db, SessionLocal
from app.models import User
from app.auth import hash_password

init_db()

email = input("Admin email: ").strip().lower()
full_name = input("Full name: ").strip()
password = input("Password (min 8 chars): ").strip()

if len(password) < 8:
    print("Password too short.")
    sys.exit(1)

db = SessionLocal()
if db.query(User).filter(User.email == email).first():
    print("User already exists.")
    db.close()
    sys.exit(1)

admin = User(
    email=email,
    full_name=full_name,
    password_hash=hash_password(password),
    is_admin=True,
    is_approved=True,
)
db.add(admin)
db.commit()
db.close()
print(f"Admin created: {email}")
