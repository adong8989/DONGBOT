from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_member(line_user_id):
    return supabase.table('members').select('*').eq('line_user_id', line_user_id).single().execute()

def add_member(line_user_id, code):
    return supabase.table('members').insert({'line_user_id': line_user_id, 'code': code, 'status': 'active'}).execute()

def get_rooms(filter_func=None):
    query = supabase.table('rooms').select('*')
    if filter_func:
        query = filter_func(query)
    return query.execute().data
