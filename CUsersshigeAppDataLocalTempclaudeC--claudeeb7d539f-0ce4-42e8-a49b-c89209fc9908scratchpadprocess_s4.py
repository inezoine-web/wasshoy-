import re

# Read the input file
input_file = r"C:\claude\wasshoy-\work\s4\s4_batch_007.tsv"
output_file = r"C:\claude\wasshoy-\work\s4\s4_verdict_007.tsv"

with open(input_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Data rows are 19-167 (150 rows)
data_lines = lines[18:]

results = []

def get_verdict(name, categories):
    """Determine if entry should be kept, dropped, or marked unsure"""
    
    # Patterns to DROP (non-events)
    drop_keywords = ['概要', 'について詳しく', '情報', '申込', '利用者', '駐車場', '交通', '医療保険', 
                     '健康保険', '支援', '運営', 'アクセス', 'サービス', '検査', '届け出', '参加要項', '募集']
    
    # Patterns that indicate KEEP
    keep_keywords = ['祭り', '祭', '花火', 'まつり', 'フェスタ', 'フェスティバル', '音楽祭', 
                     '文化祭', '踊り', '踊', '阿波踊', '盆', '正月', '神楽', '獅子舞', 
                     '民俗', '伝統', '太鼓', '和太鼓', '茶会', '能楽', '謡', 
                     '桜', '梅', '菊', '花', '市民祭', 'イベント']
    
    # First check for clear DROP patterns
    for keyword in drop_keywords:
        if keyword in name:
            # Override if also has festival keyword
            if not any(k in name for k in ['祭', 'まつり', '花火', 'フェスタ']):
                if keyword == '概要' or keyword == 'について詳しく' or keyword == '情報':
                    return 'drop', 'fragment'
                else:
                    return 'drop', 'generic'
    
    # Check categories first
    if categories:
        if 'shrine_festival' in categories or 'shrine' in categories:
            return 'keep', 'shrine'
        elif 'temple_festival' in categories or 'temple' in categories:
            return 'keep', 'temple'
        elif 'fireworks' in categories:
            return 'keep', 'fireworks'
        elif 'citizen_festival' in categories or 'citizen' in categories:
            return 'keep', 'citizen'
        elif 'seasonal' in categories:
            return 'keep', 'seasonal'
        elif 'traditional_performing_art' in categories or 'performing_art' in categories:
            return 'keep', 'folk'
        elif 'dance' in categories:
            return 'keep', 'folk'
    
    # Check for KEEP patterns in name
    for keyword in keep_keywords:
        if keyword in name:
            if '花火' in name:
                return 'keep', 'fireworks'
            elif any(k in name for k in ['神楽', '獅子舞', '太鼓', '和太鼓', '踊り', '踊']):
                return 'keep', 'folk'
            elif any(k in name for k in ['桜', '梅', '菊', '花']):
                return 'keep', 'seasonal'
            elif '市民' in name:
                return 'keep', 'citizen'
            else:
                return 'keep', 'festival'
    
    # Check for organizational/administrative entries
    if '会' in name and not any(k in name for k in ['祭', 'まつり', '花火']):
        return 'drop', 'organization'
    
    # Check for fragment patterns
    if 'について' in name or '詳しく' in name or 'YouTube' in name:
        return 'drop', 'fragment'
    
    # If unclassified, mark unsure
    if categories == 'unclassified':
        return 'unsure', 'unclear'
    
    return 'unsure', 'unclear'

# Process each data row
for i, line in enumerate(data_lines):
    if not line.strip():
        continue
    
    parts = line.rstrip('\n').split('\t')
    if len(parts) < 7:
        continue
    
    row_id = parts[0]
    categories = parts[3]
    name = parts[2]
    
    # Determine verdict
    verdict, reason = get_verdict(name, categories)
    
    # Build result row
    result_row = [row_id, verdict, reason, '', '', '']
    results.append('\t'.join(result_row))

# Write output file
with open(output_file, 'w', encoding='utf-8') as f:
    for line in results:
        f.write(line + '\n')

print(f"Processed {len(results)} rows")
