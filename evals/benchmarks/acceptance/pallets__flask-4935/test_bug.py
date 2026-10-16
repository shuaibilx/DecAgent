from flask import Blueprint, Flask

def test_bug():
    app = Flask(__name__)
    parent = Blueprint("parent", __name__)
    child = Blueprint("child", __name__)
    child.add_url_rule("/child/", view_func=lambda: "child")
    parent.register_blueprint(child)
    app.register_blueprint(parent, subdomain="api")
    app.config["SERVER_NAME"] = "domain.tld"
    response = app.test_client().get("/child/", base_url="http://api.domain.tld")
    assert response.status_code == 200
    assert response.data == b"child"
