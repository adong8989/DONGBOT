from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_member(line_user_id):
    response = supabase.table('members').select('status').eq('line_user_id', line_user_id).maybe_single().execute()
    print("Supabase response:", response)  # 加印除錯用
    if response and hasattr(response, 'data'):
        return response.data
    if response and isinstance(response, dict) and 'data' in response:
        return response['data']
    print("Supabase response is invalid or None")
    return None


def add_member(line_user_id, code="SET2024"):
    response = supabase.table('members').insert({
        "line_user_id": line_user_id,
        "code": code,
        "status": "pending"
    }).execute()
    return response.data
