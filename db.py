from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_member(line_user_id):
    response = supabase.table('members').select('status').eq('line_user_id', line_user_id).maybe_single().execute()
    print("Supabase get_member response:", response)
    if response is None:
        print("Error: supabase response is None")
        return None
    # 一般 supabase-py 的 execute() 會回傳有 data 屬性的物件
    if hasattr(response, 'data'):
        return response.data
    # 萬一是 dict 形式
    if isinstance(response, dict) and 'data' in response:
        return response['data']
    print("Unexpected supabase response format")
    return None



def add_member(line_user_id, code="SET2024"):
    response = supabase.table('members').insert({
        "line_user_id": line_user_id,
        "code": code,
        "status": "pending"
    }).execute()
    return response.data
