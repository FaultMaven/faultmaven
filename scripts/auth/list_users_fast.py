#!/usr/bin/env python3
"""Fast user listing - queries DB directly without initializing container"""
import sys
import os
from pathlib import Path
import sqlite3

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def list_users():
    """List users directly from database"""
    db_path = project_root / "faultmaven.db"
    
    if not db_path.exists():
        print("❌ Database not found. Run './scripts/faultmaven-dev.sh start' first.")
        return False
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT user_id, username, email, roles, created_at 
            FROM users 
            WHERE deleted_at IS NULL
            ORDER BY created_at DESC
        """)
        users = cursor.fetchall()
        
        print("=" * 100)
        print(f"Found {len(users)} user(s):\n")
        
        if users:
            print(f"{'#':<4} {'USERNAME':<20} {'EMAIL':<30} {'ROLES':<20} {'USER_ID'}")
            print("-" * 100)
            
            admin_count = 0
            for idx, (user_id, username, email, roles, created_at) in enumerate(users, 1):
                roles_list = roles.split(',') if roles else []
                roles_str = ', '.join(roles_list) if roles_list else 'none'
                is_admin = 'admin' in roles_list
                if is_admin:
                    admin_count += 1
                
                admin_indicator = "👑 " if is_admin else "   "
                print(f"{admin_indicator}{idx:<4} {username:<20} {email:<30} {roles_str:<20} {user_id[:36]}")
            
            print("\n" + "=" * 100)
            print(f"Total: {len(users)} user(s) | Admins: {admin_count} | Regular: {len(users) - admin_count}")
            print("=" * 100)
        else:
            print("No users found.")
            print("\nCreate a user with: ./scripts/faultmaven-dev.sh create-user")
        
        return True
    except sqlite3.OperationalError as e:
        print(f"❌ Database error: {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    success = list_users()
    sys.exit(0 if success else 1)
