import os
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, or_
from flask_apscheduler import APScheduler  # [추가됨 1] 스케줄러 라이브러리

# 1. 경로 설정
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
DB_PATH = os.path.join(BASE_DIR, 'reservation.db')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + DB_PATH
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 2. DB 모델 정의
class Reservation(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    target_name = db.Column(db.String(100))
    user_name = db.Column(db.String(100))
    start_time = db.Column(db.String(50))
    end_time = db.Column(db.String(50))
    status = db.Column(db.String(20))
    calendar_id = db.Column(db.String(200))
    event_id = db.Column(db.String(100))

with app.app_context():
    db.create_all()

# ==========================================================
# [추가됨 2] 여기서부터 스케줄러(자동 취소) 설정 시작
# ==========================================================
scheduler = APScheduler()

def auto_cancel_no_shows():
    with app.app_context():
        # 현재 시간
        now = datetime.now()
        
        # '예약됨' 상태인 모든 예약 조회
        all_reservations = Reservation.query.filter_by(status="予約済").all()
        
        cancel_count = 0
        for res in all_reservations:
            try:
                # DB에 저장된 시간 문자열을 날짜 객체로 변환
                start_dt = datetime.fromisoformat(res.start_time)
                
                # 시작 시간으로부터 10분이 지났는지 확인
                if now > (start_dt + timedelta(minutes=10)):
                    res.status = "No-Show(自動取消)"
                    cancel_count += 1
                    print(f" -> 자동 취소됨: {res.user_name} ({res.target_name})")
            except Exception as e:
                print(f"시간 변환 오류: {e}")
        
        if cancel_count > 0:
            db.session.commit()
            print(f"[알림] 총 {cancel_count}건의 노쇼 예약이 자동 취소되었습니다.")

# 스케줄러 초기화 및 시작
scheduler.init_app(app)
scheduler.add_job(id='no_show_checker', func=auto_cancel_no_shows, trigger='interval', minutes=1)
scheduler.start()
# ==========================================================
# [끝] 스케줄러 설정 끝
# ==========================================================

@app.route('/')
def index():
    return render_template('index.html')

# [API 1] 캘린더 이벤트 가져오기
@app.route('/api/events')
def get_events():
    cal_id = request.args.get('calId')
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    
    events = Reservation.query.filter(
        Reservation.calendar_id == cal_id,
        Reservation.status.in_(['予約済', '使用中', '返却済', 'No-Show', 'No-Show(自動取消)']), # 자동취소 상태도 조회되게 추가
        Reservation.end_time > start_str,
        Reservation.start_time < end_str
    ).all()

    result = []
    for e in events:
        title = f"[{e.status}] {e.user_name}"
        # 상태별 제목 표시 방식
        if e.status == '予約済': title = f"[予約] {e.user_name}"
        elif e.status == '使用中': title = f"[使用中] {e.user_name}"
        elif e.status == 'No-Show(自動取消)': title = f"[취소] {e.user_name}" # 달력에는 [취소]로 표시

        result.append({
            'id': e.event_id,
            'title': title,
            'start': e.start_time,
            'end': e.end_time,
            'status': e.status
        })
    return jsonify(result)

# [API 2] 모든 자원 상태 확인
@app.route('/api/status_all', methods=['POST'])
def check_all_statuses():
    cal_ids = request.json.get('calIds', [])
    now_str = datetime.now().isoformat()
    results = []
    
    for cal_id in cal_ids:
        occupied = Reservation.query.filter(
            Reservation.calendar_id == cal_id,
            Reservation.status.in_(['使用中']),
            Reservation.start_time <= now_str,
            Reservation.end_time > now_str
        ).first()
        
        status = 'occupied' if occupied else 'available'
        results.append({'status': status})
        
    return jsonify(results)

# [API 3] 예약 실행
@app.route('/api/book', methods=['POST'])
def process_booking():
    data = request.json
    start_dt = data['start']
    end_dt = data['end']
    cal_id = data['calId']

    conflict = Reservation.query.filter(
        Reservation.calendar_id == cal_id,
        Reservation.status.in_(['予約済', '使用中']),
        Reservation.start_time < end_dt,
        Reservation.end_time > start_dt
    ).first()

    if conflict:
        return jsonify({'success': False, 'message': "申し訳ありません。\nタッチの差で既に予約が完了してしまいました。"})

    new_event_id = str(uuid.uuid4())
    new_res = Reservation(
        id=str(uuid.uuid4()),
        target_name=data['targetName'],
        user_name=data['userName'],
        start_time=start_dt,
        end_time=end_dt,
        status="予約済",
        calendar_id=cal_id,
        event_id=new_event_id
    )
    db.session.add(new_res)
    db.session.commit()
    
    return jsonify({'success': True, 'message': "予約が完了しました。", 'eventId': new_event_id})

# [API 4] 체크인
@app.route('/api/checkin', methods=['POST'])
def do_checkin():
    data = request.json
    res = Reservation.query.filter_by(event_id=data['eventId']).first()
    if res:
        res.status = "使用中"
        db.session.commit()
        return jsonify({'success': True, 'message': "チェックインしました。"})
    return jsonify({'success': False, 'message': "予約が見つかりません。"})

# [API 5] 반납
@app.route('/api/return', methods=['POST'])
def return_booking():
    data = request.json
    res = Reservation.query.filter_by(event_id=data['eventId']).first()
    if res:
        res.status = "返却済"
        res.end_time = datetime.now().isoformat()
        db.session.commit()
        return jsonify({'success': True, 'message': "返却処理が完了しました。"})
    return jsonify({'success': False, 'message': "予約が見つかりません。"})

# [API 6] 취소
@app.route('/api/cancel', methods=['POST'])
def cancel_booking():
    data = request.json
    res = Reservation.query.filter_by(event_id=data['eventId']).first()
    if res:
        res.status = "キャンセル"
        db.session.commit()
        return jsonify({'success': True, 'message': "予約をキャンセルしました。"})
    return jsonify({'success': False, 'message': "予約が見つかりません。"})

# [API 7] 이력 조회
@app.route('/api/history')
def get_history():
    target_name = request.args.get('targetName')
    page = int(request.args.get('page', 0))
    limit = 10
    
    query = Reservation.query.filter(Reservation.target_name == target_name)
    
    start_date = request.args.get('startDate')
    end_date = request.args.get('endDate')
    if start_date: query = query.filter(Reservation.start_time >= start_date)
    if end_date: query = query.filter(Reservation.start_time <= end_date + "T23:59:59")
    
    total_count = query.count()
    history = query.order_by(Reservation.start_time.desc()).offset(page*limit).limit(limit).all()
    
    data = []
    for h in history:
        try:
            s_dt = datetime.fromisoformat(h.start_time)
            e_dt = datetime.fromisoformat(h.end_time)
            date_str = s_dt.strftime('%Y/%m/%d')
            time_str = f"{s_dt.strftime('%H:%M')} ~ {e_dt.strftime('%H:%M')}"
        except:
            date_str, time_str = h.start_time, h.end_time

        data.append({
            'date': date_str,
            'time': time_str,
            'user': h.user_name,
            'status': h.status
        })

    import math
    return jsonify({
        'data': data,
        'currentPage': page,
        'totalPages': math.ceil(total_count / limit)
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
