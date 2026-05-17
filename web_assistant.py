from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
import os
import requests
from datetime import datetime, timedelta
import threading
import time
import re
from openai import OpenAI

app = Flask(__name__)
CORS(app)

# ==================== 配置 ====================
DEEPSEEK_API_KEY = "sk-5e259e0fcd934cc3ad6d33572252b1b1"

ERROR_NOTEBOOK_FILE = "error_notebook.json"
MASTERED_FILE = "mastered_questions.json"

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# 遗忘曲线复习间隔（天）：1天、2天、4天、7天、15天、30天
REVIEW_INTERVALS = [1, 2, 4, 7, 15, 30]

# ==================== 存储 ====================
current_questions = []
current_scene = ""

def load_json(file, default):
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(file, data):
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==================== 遗忘曲线计算 ====================
def calculate_next_review(review_count, score):
    """根据复习次数和得分计算下次复习时间"""
    if score >= 7:
        if review_count >= len(REVIEW_INTERVALS):
            return None, True
        interval = REVIEW_INTERVALS[review_count]
        next_review = datetime.now() + timedelta(days=interval)
        return next_review, False
    else:
        interval = REVIEW_INTERVALS[0]
        next_review = datetime.now() + timedelta(days=interval)
        return next_review, False

def update_error_item(item_id, score):
    """更新错题本的复习计划"""
    notebook = load_json(ERROR_NOTEBOOK_FILE, [])
    for i, item in enumerate(notebook):
        if item.get("id") == item_id:
            review_count = item.get("review_count", 0) + 1
            next_review, mastered = calculate_next_review(review_count, score)
            
            if mastered or score >= 7:
                mastered_item = item.copy()
                mastered_item["mastered_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                mastered_item["final_score"] = score
                mastered_item["review_count"] = review_count
                
                mastered_list = load_json(MASTERED_FILE, [])
                mastered_list.append(mastered_item)
                save_json(MASTERED_FILE, mastered_list)
                
                notebook.pop(i)
                save_json(ERROR_NOTEBOOK_FILE, notebook)
                print(f"✅ 题目 #{item_id} 已掌握，移入已掌握库")
                return "mastered"
            else:
                notebook[i]["review_count"] = review_count
                notebook[i]["next_review"] = next_review.strftime("%Y-%m-%d %H:%M")
                notebook[i]["last_score"] = score
                save_json(ERROR_NOTEBOOK_FILE, notebook)
                print(f"📚 题目 #{item_id} 复习次数:{review_count}, 下次复习:{next_review.strftime('%Y-%m-%d')}")
                return "updated"
    return "not_found"

def get_due_review_items():
    """获取到期的复习题目（按遗忘曲线）"""
    notebook = load_json(ERROR_NOTEBOOK_FILE, [])
    now = datetime.now()
    due_items = []
    
    for item in notebook:
        next_review_str = item.get("next_review")
        if not next_review_str:
            next_review = datetime.now() + timedelta(days=1)
            item["next_review"] = next_review.strftime("%Y-%m-%d %H:%M")
            due_items.append(item)
        else:
            try:
                next_review = datetime.strptime(next_review_str, "%Y-%m-%d %H:%M")
                if next_review <= now:
                    due_items.append(item)
            except:
                due_items.append(item)
    
    due_items.sort(key=lambda x: x.get("next_review", "2099-12-31"))
    return due_items

def get_all_error_items():
    """获取所有错题（得分<7，未掌握）"""
    return load_json(ERROR_NOTEBOOK_FILE, [])

def get_mastered_items():
    """获取所有已掌握的题目"""
    return load_json(MASTERED_FILE, [])

# ==================== 智能出题核心逻辑 ====================
def get_weakness_analysis():
    """分析用户薄弱点（从错题本中提取）"""
    notebook = load_json(ERROR_NOTEBOOK_FILE, [])
    if not notebook:
        return "初级", "基础日常", "暂无薄弱点"
    
    scene_stats = {}
    for item in notebook:
        scene = item.get('scene', '通用')
        scene_stats[scene] = scene_stats.get(scene, 0) + 1
    
    if scene_stats:
        weak_scene = max(scene_stats, key=scene_stats.get)
    else:
        weak_scene = "基础日常"
    
    total_errors = len(notebook)
    if total_errors < 5:
        level = "初级"
    elif total_errors < 15:
        level = "中级"
    else:
        level = "进阶"
    
    return level, weak_scene, f"在「{weak_scene}」场景较弱，有{scene_stats.get(weak_scene, 0)}道错题"

def is_question_duplicate(new_question, existing_questions):
    """检查新题目是否与已有题目重复"""
    new_normalized = new_question.lower().strip().replace(' ', '').replace('，', ',').replace('。', '.')
    for existing in existing_questions:
        existing_normalized = existing.lower().strip().replace(' ', '').replace('，', ',').replace('。', '.')
        if new_normalized in existing_normalized or existing_normalized in new_normalized:
            return True
        new_keywords = set(re.findall(r'[\u4e00-\u9fa5]{2,}', new_normalized))
        existing_keywords = set(re.findall(r'[\u4e00-\u9fa5]{2,}', existing_normalized))
        if len(new_keywords & existing_keywords) >= 3:
            return True
    return False

def generate_questions(scene, count=10):
    """智能生成翻译题（根据用户水平和去重）"""
    level, weak_scene, weakness_desc = get_weakness_analysis()
    
    target_scene = scene if scene != "跨境电商" else weak_scene
    if scene == "跨境电商" and weak_scene != "初级":
        target_scene = weak_scene
    
    error_items = load_json(ERROR_NOTEBOOK_FILE, [])
    mastered_items = load_json(MASTERED_FILE, [])
    existing_chinese = []
    for item in error_items:
        existing_chinese.append(item.get('chinese', ''))
    for item in mastered_items:
        existing_chinese.append(item.get('chinese', ''))
    
    level_hint = ""
    if level == "初级":
        level_hint = "使用简单词汇和短句"
    elif level == "中级":
        level_hint = "使用常用商务词汇和中等长度句子"
    else:
        level_hint = "使用专业术语和复杂句式"
    
    prompt = f"""请根据以下信息生成{count}句汉译英练习题。

【用户水平】{level}（{level_hint}）
【薄弱场景】{weakness_desc}
【练习场景】{target_scene}

【已做过的题目】请严格避免重复：
{chr(10).join(['- ' + c[:30] for c in existing_chinese[-10:]])}

要求：
1. 针对用户薄弱场景进行强化
2. 难度匹配用户水平
3. 句子实用、贴近工作生活
4. 只输出中文句子，每句一行，不要有任何英文、序号、解释

请直接输出{count}句中文："""
    
    result = call_deepseek(prompt, temperature=0.8)
    lines = [line.strip() for line in result.split('\n') if line.strip() and not line.startswith('===') and len(line.strip()) > 5]
    lines = lines[:count]
    
    unique_lines = []
    for line in lines:
        if not is_question_duplicate(line, existing_chinese + unique_lines):
            unique_lines.append(line)
    
    if len(unique_lines) < count:
        fallback_lines = [
            "请查收附件中的报价单。",
            "这个产品的交货期是多久？",
            "感谢您对我们产品的关注。",
            "我们可以在月底前安排发货。",
            "请提供一份详细的规格说明。"
        ]
        while len(unique_lines) < count and fallback_lines:
            unique_lines.append(fallback_lines.pop(0))
    
    lines = unique_lines[:count]
    
    ref_prompt = f"""请为以下中文句子提供标准英文翻译，只输出英文，每句一行：

{chr(10).join(lines)}"""
    
    ref_result = call_deepseek(ref_prompt, temperature=0.5)
    refs = [ref.strip() for ref in ref_result.split('\n') if ref.strip()]
    
    questions = []
    for i, ch in enumerate(lines):
        questions.append({
            "id": i,
            "chinese": ch,
            "reference": refs[i] if i < len(refs) else "",
            "answered": False,
            "user_answer": ""
        })
    
    print(f"🎯 智能出题：水平={level}, 场景={target_scene}, 生成{len(questions)}道新题")
    return questions

# ==================== API调用 ====================
def call_deepseek(prompt, temperature=0.7):
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"API调用失败: {e}"

def grade_answer(chinese, user_answer, reference):
    """科学批改 - 基于明确评分标准"""
    
    prompt = f"""你是一位公正、严谨的英语翻译评分专家。请严格按照以下评分标准对翻译进行打分。

📊 评分标准（总分10分）

【1. 意思表达准确性 - 4分】
   4分：完全准确传达了原文意思，无遗漏或曲解
   3分：核心意思正确，但有微小偏差或遗漏
   2分：主要意思正确，但部分内容表达不清
   1分：意思有较大偏差或误解
   0分：完全错误或无法理解

【2. 语法正确性 - 3分】
   3分：语法完全正确
   2分：有1-2处小语法错误
   1分：有3-4处语法错误
   0分：语法错误严重，影响理解

【3. 用词恰当性 - 2分】
   2分：词汇选择准确、地道
   1分：基本正确但不够地道
   0分：用词错误或不恰当

【4. 表达流畅度 - 1分】
   1分：表达自然流畅
   0.5分：略显生硬或中式英语
   0分：表达混乱

📝 待批改内容

【中文原文】{chinese}

【用户译文】{user_answer}

【标准译文】{reference}

📤 输出要求（只输出JSON）

{{
    "meaning_score": 4,
    "grammar_score": 3,
    "word_score": 2,
    "fluency_score": 1,
    "total_score": 10,
    "score_reason": "详细评分理由",
    "errors": "错误点，没有写'无'",
    "suggestions": "优化建议",
    "corrected": "正确表达",
    "knowledge": "知识点讲解",
    "praise": "鼓励的话"
}}

⚠️ 重要：用户译文与标准译文意思相同但表达不同时，应给高分（8-10分）"""
    
    result = call_deepseek(prompt, temperature=0.3)
    
    try:
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            data = json.loads(json_match.group())
            
            meaning = min(max(float(data.get("meaning_score", 3)), 0), 4)
            grammar = min(max(float(data.get("grammar_score", 2)), 0), 3)
            word = min(max(float(data.get("word_score", 1.5)), 0), 2)
            fluency = min(max(float(data.get("fluency_score", 0.5)), 0), 1)
            total_score = meaning + grammar + word + fluency
            
            if total_score < 6 and len(user_answer) > 5:
                user_lower = user_answer.lower().replace(' ', '').replace('.', '').replace(',', '')
                ref_lower = reference.lower().replace(' ', '').replace('.', '').replace(',', '')
                if user_lower == ref_lower:
                    total_score = 10
                elif user_lower in ref_lower or ref_lower in user_lower:
                    total_score = max(total_score, 8)
            
            return {
                "errors": data.get("errors", "无"),
                "suggestions": data.get("suggestions", "无"),
                "corrected": data.get("corrected", user_answer),
                "score": round(total_score, 1),
                "score_reason": data.get("score_reason", ""),
                "knowledge": data.get("knowledge", "无"),
                "praise": data.get("praise", "继续加油！")
            }
    except Exception as e:
        print(f"解析批改结果失败: {e}")
    
    if user_answer.strip().lower() == reference.strip().lower():
        return {
            "errors": "无",
            "suggestions": "译文质量很好，继续保持",
            "corrected": user_answer,
            "score": 10,
            "score_reason": "与标准译文完全一致",
            "knowledge": "翻译准确",
            "praise": "完美！继续保持！"
        }
    
    return {
        "errors": "请对比标准译文",
        "suggestions": "建议多练习",
        "corrected": reference,
        "score": 6,
        "score_reason": "请对比标准答案学习",
        "knowledge": "对比学习",
        "praise": "继续努力！"
    }

def save_to_error_notebook(scene, chinese, user_answer, reference, correction):
    """保存到错题本（得分<7）"""
    notebook = load_json(ERROR_NOTEBOOK_FILE, [])
    
    score = correction.get('score', 5)
    
    if score >= 7:
        print(f"✅ 得分 {score}/10 >= 7，不保存到错题本")
        return None
    
    next_review = datetime.now() + timedelta(days=1)
    
    record = {
        "id": len(notebook) + 1,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scene": scene,
        "chinese": chinese,
        "user_answer": user_answer,
        "reference": reference,
        "errors": correction.get("errors", ""),
        "suggestions": correction.get("suggestions", ""),
        "corrected": correction.get("corrected", reference),
        "knowledge": correction.get("knowledge", ""),
        "score": score,
        "review_count": 0,
        "next_review": next_review.strftime("%Y-%m-%d %H:%M"),
        "last_score": score,
        "score_reason": correction.get("score_reason", "")
    }
    notebook.append(record)
    save_json(ERROR_NOTEBOOK_FILE, notebook)
    print(f"📝 已保存错题 #{record['id']}: {chinese[:30]}... (得分:{score}/10)")
    return record

# ==================== API路由 ====================
@app.route('/')
def index():
    return send_from_directory('.', 'web_index.html')

@app.route('/api/generate', methods=['POST'])
def api_generate():
    global current_questions, current_scene
    data = request.json
    scene = data.get('scene', '职场通用')
    count = min(data.get('count', 10), 20)
    
    current_questions = generate_questions(scene, count)
    current_scene = scene
    
    questions_data = [{"id": q["id"], "chinese": q["chinese"]} for q in current_questions]
    return jsonify({"success": True, "questions": questions_data, "scene": scene})

@app.route('/api/grade', methods=['POST'])
def api_grade():
    data = request.json
    q_id = data.get('id')
    user_answer = data.get('answer', '')
    
    if q_id >= len(current_questions):
        return jsonify({"success": False, "error": "题目不存在"})
    
    q = current_questions[q_id]
    correction = grade_answer(q['chinese'], user_answer, q['reference'])
    
    q['answered'] = True
    q['user_answer'] = user_answer
    
    score = correction.get('score', 5)
    print(f"📊 评分: {score}/10 - {correction.get('score_reason', '')}")
    
    if score < 7:
        save_to_error_notebook(current_scene, q['chinese'], user_answer, q['reference'], correction)
    
    return jsonify({"success": True, "correction": correction})

@app.route('/api/due_review_items', methods=['GET'])
def api_due_review_items():
    items = get_due_review_items()
    return jsonify({"success": True, "items": items})

@app.route('/api/all_error_items', methods=['GET'])
def api_all_error_items():
    items = get_all_error_items()
    return jsonify({"success": True, "items": items})

@app.route('/api/mastered_items', methods=['GET'])
def api_mastered_items():
    items = get_mastered_items()
    return jsonify({"success": True, "items": items})

@app.route('/api/review_answer', methods=['POST'])
def api_review_answer():
    data = request.json
    item_id = data.get('id')
    user_answer = data.get('answer', '')
    
    notebook = load_json(ERROR_NOTEBOOK_FILE, [])
    item = None
    for i in notebook:
        if i.get('id') == item_id:
            item = i
            break
    
    if not item:
        return jsonify({"success": False, "error": "题目不存在"})
    
    correction = grade_answer(item['chinese'], user_answer, item.get('reference', ''))
    score = correction.get('score', 5)
    
    result = update_error_item(item_id, score)
    
    return jsonify({"success": True, "correction": correction, "score": score, "action": result})

@app.route('/api/report', methods=['GET'])
def api_report():
    error_items = get_all_error_items()
    mastered_items = get_mastered_items()
    due_items = get_due_review_items()
    
    return jsonify({
        "success": True,
        "error_count": len(error_items),
        "mastered_count": len(mastered_items),
        "due_count": len(due_items)
    })

@app.route('/api/clear_error_items', methods=['POST'])
def api_clear_error_items():
    try:
        save_json(ERROR_NOTEBOOK_FILE, [])
        print("🗑️ 已清空错题本")
        return jsonify({"success": True, "message": "错题本已清空"})
    except Exception as e:
        return jsonify({"success": False, "message": f"清空失败: {e}"})

# ==================== 启动 ====================
if __name__ == '__main__':
    print("=" * 60)
    print("🌍 英语翻译教练 Web版已启动")
    print("=" * 60)
    print("📋 规则说明：")
    print("   ✅ 得分 < 7 → 保存到错题本")
    print("   ✅ 得分 ≥ 7 → 不保存或移入已掌握")
    print("   ✅ 复习按遗忘曲线：1天→2天→4天→7天→15天→30天")
    print("   ✅ 智能出题：根据错题本分析薄弱点")
    print("   ✅ 题目去重：避免重复练习")
    print("=" * 60)
    print("🌐 打开浏览器: http://localhost:5000")
    print("=" * 60)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
