from flask import Flask, render_template

app = Flask(__name__)

COUNTRIES = [
    {"name": "百鍊流金國", "element": "金", "desc": "初始幸運值較高，閃避與命中俱佳"},
    {"name": "翡翠靈木國", "element": "木", "desc": "生生不息之地"},
    {"name": "蔚藍千泉國", "element": "水", "desc": "以柔克剛之邦"},
    {"name": "紅蓮業火國", "element": "火", "desc": "烈焰焚天之國"},
    {"name": "萬物母育國", "element": "土", "desc": "厚德載物之土"},
]


@app.route("/")
def index():
    return render_template("index.html", countries=COUNTRIES)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
