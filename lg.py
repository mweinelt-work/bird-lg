#!/usr/bin/python
# -*- coding: utf-8 -*-
# vim: ts=4
###
#
# Copyright (c) 2012 Mehdi Abaakouk
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
#
###

import ipaddr
import subprocess
import re
from urllib2 import urlopen
from urllib import quote, unquote
import json
import random

from toolbox import mask_is_valid, ipv6_is_valid, ipv4_is_valid, resolve, save_cache_pickle, load_cache_pickle, get_asn_from_as

import pydot
from flask import Flask, render_template, jsonify, redirect, session, request, abort, Response

app = Flask(__name__)
app.config.from_pyfile('lg.cfg')


def add_links(text):
    """Browser a string and replace ipv4, ipv6, as number, with a
    whois link """

    if type(text) in [str, unicode]:
        text = text.split("\n")

    ret_text = []
    for line in text:
        # Some heuristic to create link
        if line.strip().startswith("BGP.as_path:") or \
            line.strip().startswith("Neighbor AS:"):
            ret_text.append(re.sub(r'(\d+)', r'<a href="/whois/\1" class="whois">\1</a>', line))
        else:
            line = re.sub(r'([a-zA-Z0-9\-]*\.([a-zA-Z]{2,3}){1,2})(\s|$)', r'<a href="/whois/\1" class="whois">\1</a>\3', line)
            line = re.sub(r'AS(\d+)', r'<a href="/whois/\1" class="whois">AS\1</a>', line)
            line = line.replace(' unreachable ', '\n', 1)
            line = re.sub(r'(\d+\.\d+\.\d+\.\d+)', r'<a href="/whois/\1" class="whois">\1</a>', line)
            hosts = "/".join(request.path.split("/")[2:])
            line = re.sub(r'\[(\w+)\s+((|\d\d\d\d-\d\d-\d\d\s)(|\d\d:)\d\d:\d\d|\w\w\w\d\d)', r'[<a href="/detail/%s?q=\1">\1</a> \2' % hosts, line)
            line = re.sub(r'(^|\s+)(([a-f\d]{0,4}:){3,10}[a-f\d]{0,4})', r'\1<a href="/whois/\2" class="whois">\2</a>', line, re.I)
            ret_text.append(line)
    return "\n".join(ret_text)


def set_session(request_type, hosts, proto, request_args):
    """ Store all data from user in the user session """
    session.permanent = True
    session.update({
        "request_type": request_type,
        "hosts": hosts,
        "proto": proto,
        "request_args": request_args,
    })
    history = session.get("history", [])

    # erase old format history
    if type(history) != type(list()):
        history = []

    t = (hosts, proto, request_type, request_args)
    if t in history:
        del history[history.index(t)]
    history.insert(0, t)
    session["history"] = history[:20]


def whois_command(query):
    return subprocess.Popen(['whois', query], stdout=subprocess.PIPE).communicate()[0].decode('utf-8', 'ignore')


def bird_command(host, proto, query):
    """Alias to bird_proxy for bird service"""
    return bird_proxy(host, proto, "bird", query)


def bird_proxy(host, proto, service, query):
    """Retreive data of a service from a running lg-proxy on a remote node

    First and second arguments are the node and the port of the running lg-proxy
    Third argument is the service, can be "traceroute" or "bird"
    Last argument, the query to pass to the service

    return tuple with the success of the command and the returned data
    """

    path = ""
    if proto == "ipv6":
        path = service + "6"
    elif proto == "ipv4":
        path = service

    port = app.config["PROXY"].get(host, "")

    if not port or not path:
        return False, "Host/Proto not allowed"
    else:
        if host == 'lg02':
            host = 'lg01'
        url = "http://%s.%s:%d/%s?q=%s" % (host, app.config["DOMAIN"], port, path, quote(query))
        try:
            f = urlopen(url)
            resultat = f.read()
            status = True                # retreive remote status
        except IOError:
            resultat = "Failed retreive url: %s" % url
            status = False
        return status, resultat

@app.context_processor
def inject_commands():
    commands = [
            ("summary", "show protocols"),
            ("detail", "show protocols ... all"),
            ("prefix_detail", "show route for ... all"),
            ("prefix_bgpmap", "show route for ... (bgpmap)"),
        ]
    commands_dict = {}
    for id, text in commands:
        commands_dict[id] = text
    return dict(commands=commands, commands_dict=commands_dict)

@app.context_processor
def inject_all_host():
    return dict(all_hosts="+".join(app.config["PROXY"].keys()))


@app.route("/")
def hello():
    return redirect("/prefix_detail/%s/ipv4?q=www.nlnog.net" % "+".join(app.config["PROXY"].keys()))


@app.route("/query/<path:query>")
def pfx_query(query):
    try:
        ipaddr.IPNetwork(query)
    except ValueError:
        return "Meh"
    print query
    if ipaddr.IPNetwork(query).version == 4:
        return redirect("/prefix_detail/%s/ipv4?q=%s" %
            ("+".join(app.config["PROXY"].keys()), query))
    else:
        return redirect("/prefix_detail/%s/ipv6?q=%s" % 
            ("+".join(app.config["PROXY"].keys()), query))


def error_page(text):
    return render_template('error.html', error=text), 500


@app.errorhandler(400)
def incorrect_request(e):
        return render_template('error.html', warning="The server could not understand the request"), 400


@app.errorhandler(404)
def page_not_found(e):
        return render_template('error.html', warning="The requested URL was not found on the server."), 404


@app.route("/whois/<query>")
def whois(query):
    if not query.strip():
        abort(400)

    try:
        asnum = int(query)
        query = "as%d" % asnum
    except:
        m = re.match(r"[\w\d-]*\.(?P<domain>[\d\w-]+\.[\d\w-]+)$", query)
        if m:
            query = query.groupdict()["domain"]

    output = whois_command(query).replace("\n", "<br>")
    return jsonify(output=output, title=query)


SUMMARY_UNWANTED_PROTOS = ["Kernel", "Static", "Device"]
SUMMARY_RE_MATCH = r"(?P<name>[\w_]+)\s+(?P<proto>\w+)\s+(?P<table>\w+)\s+(?P<state>\w+)\s+(?P<since>((|\d\d\d\d-\d\d-\d\d\s)(|\d\d:)\d\d:\d\d|\w\w\w\d\d))($|\s+(?P<info>.*))"


@app.route("/summary/<hosts>")
@app.route("/summary/<hosts>/<proto>")
def summary(hosts, proto="ipv4"):
    set_session("summary", hosts, proto, "")
    command = "show protocols"

    summary = {}
    error = []
    for host in hosts.split("+"):
        ret, res = bird_command(host, proto, command)
        res = res.split("\n")
        if len(res) > 1:
            data = []
            for line in res[1:]:
                line = line.strip()
                if line and (line.split() + [""])[1] not in SUMMARY_UNWANTED_PROTOS:
                    m = re.match(SUMMARY_RE_MATCH, line)
                    if m:
                        data.append(m.groupdict())
                    else:
                        app.logger.warning("couldn't parse: %s", line)

            summary[host] = data
        else:
            error.append("%s: bird command failed with error, %s" % (host, "\n".join(res)))
    return render_template('summary.html', summary=summary, command=command, error="<br>".join(error))


@app.route("/detail/<hosts>/<proto>")
def detail(hosts, proto):
    name = request.args.get('q', '').strip()
    if not name:
        abort(400)

    set_session("detail", hosts, proto, name)
    command = "show protocols all %s" % name

    detail = {}
    error = []
    for host in hosts.split("+"):
        ret, res = bird_command(host, proto, command)
        res = res.split("\n")
        if len(res) > 1:
            detail[host] = {"status": res[1], "description": add_links(res[2:])}
        else:
            error.append("%s: bird command failed with error, %s" % (host, "\n".join(res)))

    return render_template('detail.html', detail=detail, command=command, error="<br>".join(error))


@app.route("/traceroute/<hosts>/<proto>")
def traceroute(hosts, proto):
    q = request.args.get('q', '').strip()
    if not q:
        abort(400)

    set_session("traceroute", hosts, proto, q)

    if proto == "ipv6" and not ipv6_is_valid(q):
        try:
            q = resolve(q, "AAAA")
        except:
            return error_page("%s is unresolvable or invalid for %s" % (q, proto))
    if proto == "ipv4" and not ipv4_is_valid(q):
        try:
            q = resolve(q, "A")
        except:
            return error_page("%s is unresolvable or invalid for %s" % (q, proto))

    infos = {}
    for host in hosts.split("+"):
        status, resultat = bird_proxy(host, proto, "traceroute", q)
        infos[host] = add_links(resultat)
    return render_template('traceroute.html', infos=infos)


@app.route("/adv/<hosts>/<proto>")
def show_route_filter(hosts, proto):
    return show_route("adv", hosts, proto)


@app.route("/adv_bgpmap/<hosts>/<proto>")
def show_route_filter_bgpmap(hosts, proto):
    return show_route("adv_bgpmap", hosts, proto)


@app.route("/where/<hosts>/<proto>")
def show_route_where(hosts, proto):
    return show_route("where", hosts, proto)


@app.route("/where_detail/<hosts>/<proto>")
def show_route_where_detail(hosts, proto):
    return show_route("where_detail", hosts, proto)


@app.route("/where_bgpmap/<hosts>/<proto>")
def show_route_where_bgpmap(hosts, proto):
    return show_route("where_bgpmap", hosts, proto)


@app.route("/prefix/<hosts>/<proto>")
def show_route_for(hosts, proto):
    return show_route("prefix", hosts, proto)


@app.route("/prefix_detail/<hosts>/<proto>")
def show_route_for_detail(hosts, proto):
    return show_route("prefix_detail", hosts, proto)


@app.route("/prefix_bgpmap/<hosts>/<proto>")
def show_route_for_bgpmap(hosts, proto):
    return show_route("prefix_bgpmap", hosts, proto)


ASNAME_CACHE_FILE = "/tmp/asname_cache.pickle"
ASNAME_CACHE = load_cache_pickle(ASNAME_CACHE_FILE, {})

def get_as_name(_as):
    """return a string that contain the as number following by the as name

    It's the use whois database informations
    # Warning, the server can be blacklisted from ripe is too many requests are done
    """
    if not _as:
        return "AS?????"

    if not _as.isdigit():
        return _as.strip()

    name = get_asn_from_as(_as)[-1].replace(" ","\r",1)
    return "AS%s | %s" % (_as, name)

    if _as not in ASNAME_CACHE:
        whois_answer = whois_command("as%s" % _as)
        as_name = re.search('(as-name|ASName): (.*)', whois_answer)
        if as_name:
            ASNAME_CACHE[_as] = as_name.group(2).strip()
        else:
            ASNAME_CACHE[_as] = _as
    save_cache_pickle(ASNAME_CACHE_FILE, ASNAME_CACHE)
    if ASNAME_CACHE[_as] == _as:
        return "AS%s" % _as
    else:
        return "AS%s\r%s" % (_as, ASNAME_CACHE[_as])

def get_as_number_from_protocol_name(host, proto, protocol):
    ret, res = bird_command(host, proto, "show protocols all %s" % protocol)
    re_asnumber = re.search("Neighbor AS:\s*(\d*)", res)
    if re_asnumber:
        return re_asnumber.group(1)
    else:
        return "?????"


@app.route("/bgpmap/")
def show_bgpmap():
    """return a bgp map in a png file, from the json tree in q argument"""

    data = request.args.get('q', '').strip()
    if not data:
        abort(400)

    data = json.loads(unquote(data))

    graph = pydot.Dot('BGPMAP', graph_type='digraph')

    nodes = {}
    edges = {}

	
    def escape(label):
        label = label.replace("&", "&amp;")
        label = label.replace(">", "&gt;")
        label = label.replace("<", "&lt;")
        return label


    def add_node(_as, **kwargs):
        if _as not in nodes:
#            kwargs["label"] = '<<TABLE CELLBORDER="0" BORDER="0" CELLPADDING="0" CELLSPACING="0"><TR><TD ALIGN="CENTER">' + kwargs.get("label", get_as_name(_as)).replace("\r","<BR/>") + "</TD></TR></TABLE>>"
            kwargs["label"] = '<<TABLE CELLBORDER="0" BORDER="0" CELLPADDING="0" CELLSPACING="0"><TR><TD ALIGN="CENTER">' + escape(kwargs.get("label", get_as_name(_as))).replace("\r","<BR/>") + "</TD></TR></TABLE>>"
            nodes[_as] = pydot.Node(_as, style="filled", fontsize="10", **kwargs)
            graph.add_node(nodes[_as])
        return nodes[_as]

    def add_edge(_previous_as, _as, **kwargs):
        kwargs["splines"] = "true"
        force = kwargs.get("force", False)

        edge_tuple = (_previous_as, _as)
        if force or edge_tuple not in edges:
            edge = pydot.Edge(*edge_tuple, **kwargs)
            graph.add_edge(edge)
            edges[edge_tuple] = edge
        elif "label" in kwargs and kwargs["label"]:
            e = edges[edge_tuple]

            label_without_star = kwargs["label"].replace("*", "")
            labels = e.get_label().split("\r") 
            if "%s*" % label_without_star not in labels:
                labels = [ kwargs["label"] ]  + [ l for l in labels if not l.startswith(label_without_star) ] 
                labels = sorted(labels, cmp=lambda x,y: x.endswith("*") and -1 or 1)

#                e.set_label("\r".join(labels))
                label = escape("\r".join(labels))
                e.set_label(label)
        return edges[edge_tuple]

    for host, asmaps in data.iteritems():
        add_node(host, label= "%s\r%s" % (host.upper(), app.config["DOMAIN"].upper()), shape="box", fillcolor="#F5A9A9")

        as_number = app.config["AS_NUMBER"].get(host, None)
        if as_number:
            node = add_node(as_number, fillcolor="#F5A9A9")
            edge = add_edge(as_number, nodes[host])
            edge.set_color("red")
            edge.set_style("bold")
    
    #colors = [ "#009e23", "#1a6ec1" , "#d05701", "#6f879f", "#939a0e", "#0e9a93", "#9a0e85", "#56d8e1" ]
    previous_as = None
    hosts = data.keys()
    for host, asmaps in data.iteritems():
        first = True
        for asmap in asmaps:
            previous_as = host
            color = "#%x" % random.randint(0, 16777215)

            hop = False
            hop_label = ""
            for _as in asmap:
                if _as == previous_as:
                    continue

                if not hop:
                    hop = True
                    if _as not in hosts:
                        hop_label = _as 
                        if first:
                            hop_label = hop_label + "*"
                        continue
                    else:
                        hop_label = ""

                
                add_node(_as, fillcolor=(first and "#F5A9A9" or "white"))
                edge = add_edge(nodes[previous_as], nodes[_as] , label=hop_label, fontsize="7")

                hop_label = ""

                if first:
                    edge.set_style("bold")
                    edge.set_color("red")
                elif edge.get_color() != "red":
                    edge.set_style("dashed")
                    edge.set_color(color)

                previous_as = _as
            first = False

    if previous_as:
        node = add_node(previous_as)
        node.set_shape("box")

    #return Response("<pre>" + graph.create_dot() + "</pre>")
    return Response(graph.create_png(), mimetype='image/png')


def build_as_tree_from_raw_bird_ouput(host, proto, text):
    """Extract the as path from the raw bird "show route all" command"""

    path = None
    paths = []
    net_dest = None
    for line in text:
        line = line.strip()

	expr = re.search(r'(.*)unreachable\s+\[(\w+)\s+.*from\s+([0-9a-fA-F:\.]+)]', line)
#        expr = re.search(r'(.*)via\s+([0-9a-fA-F:\.]+)\s+on.*\[(\w+)\s+', line)
        if expr:
            if path:
                path.append(net_dest)
                paths.append(path)
                path = None

            if expr.group(1).strip():
                net_dest = expr.group(1).strip()

            peer_ip = expr.group(3).strip()
            peer_protocol_name = expr.group(2).strip()
            path = [ peer_protocol_name ]
#                path = ["%s\r%s" % (peer_protocol_name, get_as_name(get_as_number_from_protocol_name(host, proto, peer_protocol_name)))]
        
#        expr2 = re.search(r'(.*)unreachable\s+\[(\w+)\s+', line)
#        if expr2:
#            if path:
#                path.append(net_dest)
#                paths.append(path)
#                path = None
#                path = [ peer_protocol_name ]
#
#            if expr2.group(1).strip():
#                net_dest = expr2.group(1).strip()

        if line.startswith("BGP.as_path:"):
            path.extend(line.replace("BGP.as_path:", "").strip().split(" "))
    
    if path:
        path.append(net_dest)
        paths.append(path)

    return paths


def show_route(request_type, hosts, proto):
    expression = unquote(request.args.get('q', '')).strip()
    if not expression:
        abort(400)

    set_session(request_type, hosts, proto, expression)

    bgpmap = request_type.endswith("bgpmap")

    all = (request_type.endswith("detail") and " all" or "")
    if bgpmap:
        all = " all"

    if request_type.startswith("adv"):
        command = "show route " + expression.strip()
        if bgpmap and not command.endswith("all"):
            command = command + " all"
    elif request_type.startswith("where"):
        command = "show route where net ~ [ " + expression + " ]" + all
    else:
        mask = ""
        if len(expression.split("/")) > 1:
            expression, mask = (expression.split("/"))

        if not mask and proto == "ipv4":
            mask = "32"
        if not mask and proto == "ipv6":
            mask = "128"
        if not mask_is_valid(mask):
            return error_page("mask %s is invalid" % mask)

        if proto == "ipv6" and not ipv6_is_valid(expression):
            try:
                expression = resolve(expression, "AAAA")
            except:
                return error_page("%s is unresolvable or invalid for %s" % (expression, proto))
        if proto == "ipv4" and not ipv4_is_valid(expression):
            try:
                expression = resolve(expression, "A")
            except:
                return error_page("%s is unresolvable or invalid for %s" % (expression, proto))

        if mask:
            expression += "/" + mask

        command = "show route for " + expression + all

    detail = {}
    error = []
    for host in hosts.split("+"):
        ret, res = bird_command(host, proto, command)

        res = res.split("\n")
        if len(res) > 1:
            if bgpmap:
                detail[host] = build_as_tree_from_raw_bird_ouput(host, proto, res)
            else:
                detail[host] = add_links(res)
        else:
            error.append("%s: bird command failed with error, %s" % (host, "\n".join(res)))

    if bgpmap:
        detail = json.dumps(detail)

    return render_template((bgpmap and 'bgpmap.html' or 'route.html'), detail=detail, command=command, expression=expression, error="<br />".join(error))


app.secret_key = app.config["SESSION_KEY"]
app.debug = True
if __name__ == "__main__":
    app.run("0.0.0.0")
