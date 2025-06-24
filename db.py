from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_member(line_user_id):
    response = supabase.table('members').select('status').eq('line_user_id', line_user_id).maybe_single().execute()
    return response.data

def add_member(line_user_id, code="SET2024"):
    response = supabase.table('members').insert({
        "line_user_id": line_user_id,
        "code": code,
        "status": "pending"
    }).execute()
    return response.data
