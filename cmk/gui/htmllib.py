#!/usr/bin/env python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

# TODO:
#
# Notes for future rewrite:
#
# - Find all call sites which do something like "int(html.var(...))"
#   and replace it with html.get_integer_input(...)
#
# - Make clear which functions return values and which write out values
#   render_*, add_*, write_* (e.g. icon() -> outputs directly,
#                                  render_icon() -> returns icon
#
# - Order of arguments:
#   e.g. icon(help, icon) -> change and make help otional?
#
# - Fix names of message() show_error() show_warning()
#
# - change naming of html.attrencode() to html.render()
#
# - General rules:
# 1. values of type str that are passed as arguments or
#    return values or are stored in datastructures must not contain
#    non-Ascii characters! UTF-8 encoding must just be used in
#    the last few CPU cycles before outputting. Conversion from
#    input to str or unicode must happen as early as possible,
#    directly when reading from file or URL.
#
# - indentify internal helper methods and prefix them with "_"
#
# - Split HTML handling (page generating) code and generic request
#   handling (vars, cookies, ...) up into separate classes to make
#   the different tasks clearer. For HTMLGenerator() or similar.

import time
import os
import urllib
import ast
import random
import re
import signal
import json
import abc
import pprint

from collections import deque
from contextlib import contextmanager

# Monkey patch in order to make the HTML class below json-serializable without changing the default json calls.
def _default(self, obj):
    return getattr(obj.__class__, "to_json", _default.default)(obj)
_default.default = json.JSONEncoder().default # Save unmodified default.
json.JSONEncoder.default = _default # replacement


import cmk.paths
from cmk.exceptions import MKGeneralException
from cmk.gui.exceptions import MKUserError, RequestTimeout

import cmk.gui.utils as utils
import cmk.gui.config as config
import cmk.gui.log as log
from cmk.gui.i18n import _


#.
#   .--Escaper-------------------------------------------------------------.
#   |                 _____                                                |
#   |                | ____|___  ___ __ _ _ __   ___ _ __                  |
#   |                |  _| / __|/ __/ _` | '_ \ / _ \ '__|                 |
#   |                | |___\__ \ (_| (_| | |_) |  __/ |                    |
#   |                |_____|___/\___\__,_| .__/ \___|_|                    |
#   |                                    |_|                               |
#   +----------------------------------------------------------------------+
#   |                                                                      |
#   '----------------------------------------------------------------------

class Escaper(object):
    def __init__(self):
        super(Escaper, self).__init__()
        self._unescaper_text        = re.compile(r'&lt;(/?)(h1|h2|b|tt|i|u|br(?: /)?|nobr(?: /)?|pre|a|sup|p|li|ul|ol)&gt;')
        self._unescaper_href        = re.compile(r'&lt;a href=(?:&quot;|\')(.*?)(?:&quot;|\')&gt;')
        self._unescaper_href_target = re.compile(r'&lt;a href=(?:&quot;|\')(.*?)(?:&quot;|\') target=(?:&quot;|\')(.*?)(?:&quot;|\')&gt;')


    # Encode HTML attributes. Replace HTML syntax with HTML text.
    # For example: replace '"' with '&quot;', '<' with '&lt;'.
    # This code is slow. Works on str and unicode without changing
    # the type. Also works on things that can be converted with '%s'.
    def escape_attribute(self, value):
        attr_type = type(value)
        if value is None:
            return ''
        elif attr_type == int:
            return str(value)
        elif isinstance(value, HTML):
            return "%s" % value # This is HTML code which must not be escaped
        elif attr_type not in [str, unicode]: # also possible: type Exception!
            value = "%s" % value # Note: this allows Unicode. value might not have type str now
        return value.replace("&", "&amp;")\
                    .replace('"', "&quot;")\
                    .replace("<", "&lt;")\
                    .replace(">", "&gt;")


    def unescape_attributes(self, value):
        return value.replace("&amp;", "&")\
                    .replace("&quot;", "\"")\
                    .replace("&lt;", "<")\
                    .replace("&gt;", ">")


    # render HTML text.
    # We only strip od some tags and allow some simple tags
    # such as <h1>, <b> or <i> to be part of the string.
    # This is useful for messages where we want to keep formatting
    # options. (Formerly known as 'permissive_attrencode') """
    # for the escaping functions
    def escape_text(self, text):

        if isinstance(text, HTML):
            return "%s" % text # This is HTML code which must not be escaped

        text = self.escape_attribute(text)
        text = self._unescaper_text.sub(r'<\1\2>', text)
        # Also repair link definitions
        text = self._unescaper_href_target.sub(r'<a href="\1" target="\2">', text)
        text = self._unescaper_href.sub(r'<a href="\1">', text)
        text = re.sub(r'&amp;nbsp;', '&nbsp;', text)
        return text


#.
#   .--Encoding------------------------------------------------------------.
#   |              _____                     _ _                           |
#   |             | ____|_ __   ___ ___   __| (_)_ __   __ _               |
#   |             |  _| | '_ \ / __/ _ \ / _` | | '_ \ / _` |              |
#   |             | |___| | | | (_| (_) | (_| | | | | | (_| |              |
#   |             |_____|_| |_|\___\___/ \__,_|_|_| |_|\__, |              |
#   |                                                  |___/               |
#   +----------------------------------------------------------------------+
#   |                                                                      |
#   '----------------------------------------------------------------------'

class Encoder(object):
    def urlencode_vars(self, vars):
        """Convert a mapping object or a sequence of two-element tuples to a “percent-encoded” string

        This function returns a str object, never unicode!
        Note: This should be changed once we change everything to
        unicode internally.
        """
        assert type(vars) == list
        pairs = []
        for varname, value in sorted(vars):
            assert type(varname) == str

            if type(value) == int:
                value = str(value)
            elif type(value) == unicode:
                value = value.encode("utf-8")
            elif value is None:
                # TODO: This is not ideal and should better be cleaned up somehow. Shouldn't
                # variables with None values simply be skipped? We currently can not find the
                # call sites easily. This may be cleaned up once we establish typing. Until then
                # we need to be compatible with the previous behavior.
                value = ""

            #assert type(value) == str, "%s: %s" % (varname, value)

            pairs.append((varname, value))

        return urllib.urlencode(pairs)


    def urlencode(self, value):
        """Replace special characters in string using the %xx escape.

        This function returns a str object, never unicode!
        Note: This should be changed once we change everything to
        unicode internally.
        """
        if type(value) == unicode:
            value = value.encode("utf-8")
        elif value == None:
            return ""

        assert type(value) == str

        return urllib.quote_plus(value)


#.
#   .--HTML----------------------------------------------------------------.
#   |                      _   _ _____ __  __ _                            |
#   |                     | | | |_   _|  \/  | |                           |
#   |                     | |_| | | | | |\/| | |                           |
#   |                     |  _  | | | | |  | | |___                        |
#   |                     |_| |_| |_| |_|  |_|_____|                       |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | This is a simple class which wraps a unicode string provided by      |
#   | the caller to make html.attrencode() know that this string should    |
#   | not be escaped.                                                      |
#   |                                                                      |
#   | This way we can implement encodings while still allowing HTML code.  |
#   | This is useful when one needs to print out HTML tables in messages   |
#   | or help texts.                                                       |
#   |                                                                      |
#   | The HTML class is implemented as an immutable type.                  |
#   | Every instance of the class is a unicode string.                     |
#   | Only utf-8 compatible encodings are supported.                       |
#   '----------------------------------------------------------------------'

class HTML(object):
    def __init__(self, value = u''):
        super(HTML, self).__init__()
        self.value = self._ensure_unicode(value)


    def __unicode__(self):
        return self.value


    def _ensure_unicode(self, thing, encoding_index=0):
        try:
            return unicode(thing)
        except UnicodeDecodeError:
            return thing.decode("utf-8")


    def __bytebatzen__(self):
        return self.value.encode("utf-8")


    def __str__(self):
        # Against the sense of the __str__() method, we need to return the value
        # as unicode here. Why? There are many cases where something like
        # "%s" % HTML(...) is done in the GUI code. This calls the __str__ function
        # because the origin is a str() object. The call will then return a UTF-8
        # encoded str() object. This brings a lot of compatbility issues to the code
        # which can not be solved easily.
        # To deal with this situation we need the implicit conversion from str to
        # unicode that happens when we return a unicode value here. In all relevant
        # cases this does exactly what we need. It would only fail if the origin
        # string contained characters that can not be decoded with the ascii codec
        # which is not relevant for us here.
        #
        # This is problematic:
        #   html.write("%s" % HTML("ä"))
        #
        # Bottom line: We should relly cleanup internal unicode/str handling.
        return self.value


    def __repr__(self):
        return ("HTML(\"%s\")" % self.value).encode("utf-8")


    def to_json(self):
        return self.value


    def __add__(self, other):
        return HTML(self.value + self._ensure_unicode(other))


    def __iadd__(self, other):
        return self.__add__(other)


    def __radd__(self, other):
        return HTML(self._ensure_unicode(other) + self.value)


    def join(self, iterable):
        return HTML(self.value.join(map(self._ensure_unicode, iterable)))


    def __eq__(self, other):
        return self.value == self._ensure_unicode(other)


    def __ne__(self, other):
        return self.value != self._ensure_unicode(other)


    def __len__(self):
        return len(self.value)


    def __getitem__(self, index):
        return HTML(self.value[index])


    def __contains__(self, item):
        return self._ensure_unicode(item) in self.value


    def count(self, sub, *args):
        return self.value.count(self._ensure_unicode(sub), *args)


    def index(self, sub, *args):
        return self.value.index(self._ensure_unicode(sub), *args)


    def lstrip(self, *args):
        args = tuple(map(self._ensure_unicode, args[:1])) + args[1:]
        return HTML(self.value.lstrip(*args))


    def rstrip(self, *args):
        args = tuple(map(self._ensure_unicode, args[:1])) + args[1:]
        return HTML(self.value.rstrip(*args))


    def strip(self, *args):
        args = tuple(map(self._ensure_unicode, args[:1])) + args[1:]
        return HTML(self.value.strip(*args))


    def lower(self):
        return HTML(self.value.lower())


    def upper(self):
        return HTML(self.value.upper())


    def startswith(self, prefix, *args):
        return self.value.startswith(self._ensure_unicode(prefix), *args)


#.
#   .--OutputFunnel--------------------------------------------------------.
#   |     ___        _               _   _____                       _     |
#   |    / _ \ _   _| |_ _ __  _   _| |_|  ___|   _ _ __  _ __   ___| |    |
#   |   | | | | | | | __| '_ \| | | | __| |_ | | | | '_ \| '_ \ / _ \ |    |
#   |   | |_| | |_| | |_| |_) | |_| | |_|  _|| |_| | | | | | | |  __/ |    |
#   |    \___/ \__,_|\__| .__/ \__,_|\__|_|   \__,_|_| |_|_| |_|\___|_|    |
#   |                   |_|                                                |
#   +----------------------------------------------------------------------+
#   | Provides the write functionality. The method _lowlevel_write needs   |
#   | to be overwritten in the specific subclass!                          |
#   |                                                                      |
#   |  Usage of plugged context:                                           |
#   |          with html.plugged():                                        |
#   |             html.write("something")                                  |
#   |             html_code = html.drain()                                 |
#   |          print html_code                                             |
#   '----------------------------------------------------------------------'


class OutputFunnel(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        super(OutputFunnel, self).__init__()
        self.plug_level = -1
        self.plug_text = []


    # Accepts str and unicode objects only!
    # The plugged functionality can be used for debugging.
    def write(self, text):
        if not text:
            return

        if isinstance(text, HTML):
            text = "%s" % text

        if type(text) not in [str, unicode]: # also possible: type Exception!
            raise MKGeneralException(_('Type Error: html.write accepts str and unicode input objects only!'))

        if self.is_plugged():
            self.plug_text[self.plug_level].append(text)
        else:
            # encode when really writing out the data. Not when writing plugged,
            # because the plugged code will be handled somehow by our code. We
            # only encode when leaving the pythonic world.
            if type(text) == unicode:
                text = text.encode("utf-8")
            self._lowlevel_write(text)


    @abc.abstractmethod
    def _lowlevel_write(self, text):
        raise NotImplementedError()


    @contextmanager
    def plugged(self):
        self.plug()
        try:
            yield
        except Exception, e:
            self.drain()
            raise
        finally:
            self.unplug()


    # Put in a plug which stops the text stream and redirects it to a sink.
    def plug(self):
        self.plug_text.append([])
        self.plug_level += 1


    def is_plugged(self):
        return self.plug_level > -1


    # Pull the plug for a moment to allow the sink content to pass through.
    def flush(self):
        if not self.is_plugged():
            return None

        text = "".join(self.plug_text[self.plug_level])
        self.plug_text[self.plug_level] = []
        self.plug_level -= 1
        self.write(text)
        self.plug_level += 1


    # Get the sink content in order to do something with it.
    def drain(self):
        if not self.is_plugged():
            return ''

        text = "".join(self.plug_text[self.plug_level])
        self.plug_text[self.plug_level] = []
        return text


    def unplug(self):
        if not self.is_plugged():
            return

        self.flush()
        self.plug_text.pop()
        self.plug_level -= 1


    def unplug_all(self):
        while self.is_plugged():
            self.unplug()


#.
#   .--HTML Generator------------------------------------------------------.
#   |                      _   _ _____ __  __ _                            |
#   |                     | | | |_   _|  \/  | |                           |
#   |                     | |_| | | | | |\/| | |                           |
#   |                     |  _  | | | | |  | | |___                        |
#   |                     |_| |_| |_| |_|  |_|_____|                       |
#   |                                                                      |
#   |             ____                           _                         |
#   |            / ___| ___ _ __   ___ _ __ __ _| |_ ___  _ __             |
#   |           | |  _ / _ \ '_ \ / _ \ '__/ _` | __/ _ \| '__|            |
#   |           | |_| |  __/ | | |  __/ | | (_| | || (_) | |               |
#   |            \____|\___|_| |_|\___|_|  \__,_|\__\___/|_|               |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Generator which provides top level HTML writing functionality.      |
#   '----------------------------------------------------------------------'


class HTMLGenerator(OutputFunnel):
    """ Usage Notes:

          - Tags can be opened using the open_[tag]() call where [tag] is one of the possible tag names.
            All attributes can be passed as function arguments, such as open_div(class_="example").
            However, python specific key words need to be escaped using a trailing underscore.
            One can also provide a dictionary as attributes: open_div(**{"class": "example"}).

          - All tags can be closed again using the close_[tag]() syntax.

          - For tags which shall only contain plain text (i.e. no tags other than highlighting tags)
            you can a the direct call using the tag name only as function name,
            self.div("Text content", **attrs). Tags featuring this functionality are listed in
            the "featured shortcuts" list.

          - Some tags require mandatory arguments. Those are defined explicitly below.
            For example an a tag needs the href attribute everytime.

          - If you want to provide plain HTML to a tag, please use the tag_content function or
            facillitate the HTML class.

        HOWTO HTML Attributes:

          - Python specific attributes have to be escaped using a trailing underscore

          - All attributes can be python objects. However, some attributes can also be lists of attrs:

                'class' attributes will be concatenated using one whitespace
                'style' attributes will be concatenated using the semicolon and one whitespace
                Behaviorial attributes such as 'onclick', 'onmouseover' will bec concatenated using
                a semicolon and one whitespace.

          - All attributes will be escaped, i.e. the characters '&', '<', '>', '"' will be replaced by
            non HtML relevant signs '&amp;', '&lt;', '&gt;' and '&quot;'. """

    # TODO: Replace u, i, b with underline, italic, bold, usw.

    # these tags can be called by their tag names, e.g. 'self.title(content)'
    _shortcut_tags = set(["title", "h1", "h2", "h3", "h4", "th", "tr", "td", "center", "pre", "style", "iframe",\
                          "div", "p", "span", "canvas", "strong", "sub", "tt", "u", "i", "b", "x", "option"])

    # these tags can be called by open_name(), close_name() and render_name(), e.g. 'self.open_html()'
    _tag_names = set(['html', 'head', 'body', 'header', 'footer', 'a', 'b', 'sup',\
                              'script', 'form', 'button', 'p', 'select', 'fieldset',\
                              'table', 'tbody', 'row', 'ul', 'li', 'br', 'nobr', 'input', 'span'])

    # Of course all shortcut tags can be used as well.
    _tag_names.update(_shortcut_tags)


    def __init__(self):
        super(HTMLGenerator, self).__init__()
        self.escaper = Escaper()


    #
    # Rendering
    #


    def _render_attributes(self, **attrs):
        # make class attribute foolproof
        css = []
        for k in ["class_", "css", "cssclass", "class"]:
            if k in attrs:
                if isinstance(attrs[k], list):
                    css.extend(attrs.pop(k))
                elif attrs[k] is not None:
                    css.append(attrs.pop(k))
        if css:
            attrs["class"] = css

        # options such as 'selected' and 'checked' dont have a value in html tags
        options = []

        # render all attributes
        for k, v in attrs.iteritems():

            if v is None:
                continue

            k = self.escaper.escape_attribute(k.rstrip('_'))

            if v == '':
                options.append(k)
                continue

            if not isinstance(v, list):
                v = self.escaper.escape_attribute(v)
            else:
                if k == "class":
                    sep = ' '
                elif k == "style" or k.startswith('on'):
                    sep = '; '
                else:
                    sep = '_'

                v = sep.join([a for a in (self.escaper.escape_attribute(vi) for vi in v) if a])

                if sep.startswith(';'):
                    v = re.sub(';+', ';', v)

            yield ' %s=\"%s\"' %(k, v)

        for k in options:
            yield " %s=\'\'" % k


    # applies attribute encoding to prevent code injections.
    def _render_opening_tag(self, tag_name, close_tag=False, **attrs):
        """ You have to replace attributes which are also python elements such as
            'class', 'id', 'for' or 'type' using a trailing underscore (e.g. 'class_' or 'id_'). """
        return HTML("<%s%s%s>" % (tag_name,\
                                    '' if not attrs else ''.join(self._render_attributes(**attrs)),\
                                    '' if not close_tag else ' /'))


    def _render_closing_tag(self, tag_name):
        return  HTML("</%s>" % (tag_name))


    def _render_content_tag(self, tag_name, tag_content, **attrs):
        tag = self._render_opening_tag(tag_name, **attrs)

        if tag_content in ['', None]:
            pass

        else:
            tag = tag.rstrip("\n")
            if isinstance(tag_content, HTML):
                tag += tag_content.lstrip(' ').rstrip('\n')
            else:
                tag += self.escaper.escape_text(tag_content)

        tag += "</%s>" % (tag_name)

        return HTML(tag)


    # This is used to create all the render_tag() and close_tag() functions
    def __getattr__(self, name):
        """ All closing tags can be called like this:
            self.close_html(), self.close_tr(), etc. """

        parts = name.split('_')

        # generating the shortcut tag calls
        if len(parts) == 1 and name in self._shortcut_tags:
            return lambda content, **attrs: self.write_html(self._render_content_tag(name, content, **attrs))

        # generating the open, close and render calls
        elif len(parts) == 2:
            what, tag_name = parts[0], parts[1]

            if what == "open" and tag_name in self._tag_names:
                return lambda **attrs: self.write_html(self._render_opening_tag(tag_name, **attrs))

            elif what == "close" and tag_name in self._tag_names:
                return lambda : self.write_html(self._render_closing_tag(tag_name))

            elif what == "render" and tag_name in self._tag_names:
                return lambda content, **attrs: HTML(self._render_content_tag(tag_name, content, **attrs))

        else:
            # FIXME: This returns None, which is not a very informative error message
            return object.__getattribute__(self, name)

    #
    # HTML element methods
    # If an argument is mandatory, it is used as default and it will overwrite an
    # implicit argument (e.g. id_ will overwrite attrs["id"]).
    #


    #
    # basic elements
    #


    def render_text(self, text):
        return HTML(self.escaper.escape_text(text))


    def write_text(self, text):
        """ Write text. Highlighting tags such as h2|b|tt|i|br|pre|a|sup|p|li|ul|ol are not escaped. """
        self.write(self.render_text(text))


    def write_html(self, content):
        """ Write HTML code directly, without escaping. """
        self.write(content)


    def comment(self, comment_text):
        self.write("<!--%s-->" % self.encode_attribute(comment_text))


    def meta(self, httpequiv=None, **attrs):
        if httpequiv:
            attrs['http-equiv'] = httpequiv
        self.write_html(self._render_opening_tag('meta', close_tag=True, **attrs))


    def base(self, target):
        self.write_html(self._render_opening_tag('base', close_tag=True, target=target))


    def open_a(self, href, **attrs):
        attrs['href'] = href
        self.write_html(self._render_opening_tag('a', **attrs))


    def render_a(self, content, href, **attrs):
        attrs['href'] = href
        return self._render_content_tag('a', content, **attrs)


    def a(self, content, href, **attrs):
        self.write_html(self.render_a(content, href, **attrs))


    def stylesheet(self, href):
        self.write_html(self._render_opening_tag('link', rel="stylesheet", type_="text/css", href=href, close_tag=True))


    #
    # Scriptingi
    #


    def render_javascript(self, code):
        return HTML("<script type=\"text/javascript\">\n%s\n</script>\n" % code)


    def javascript(self, code):
        self.write_html(self.render_javascript(code))


    def javascript_file(self, src):
        """ <script type="text/javascript" src="%(name)"/>\n """
        self.write_html(self._render_content_tag('script', '', type_="text/javascript", src=src))


    def render_img(self, src, **attrs):
        attrs['src'] = src
        return self._render_opening_tag('img', close_tag=True, **attrs)


    def img(self, src, **attrs):
        self.write_html(self.render_img(src, **attrs))


    def open_button(self, type_, **attrs):
        attrs['type'] = type_
        self.write_html(self._render_opening_tag('button', close_tag=True, **attrs))


    def play_sound(self, url):
        self.write_html(self._render_opening_tag('audio autoplay', src_=url))


    #
    # form elements
    #


    def render_label(self, content, for_, **attrs):
        attrs['for'] = for_
        return self._render_content_tag('label', content, **attrs)


    def label(self, content, for_, **attrs):
        self.write_html(self.render_label(content, for_, **attrs))


    def render_input(self, name, type_, **attrs):
        attrs['type_'] = type_
        attrs['name'] = name
        return self._render_opening_tag('input', close_tag=True, **attrs)


    def input(self, name, type_, **attrs):
        self.write_html(self.render_input(name, type_, **attrs))


    #
    # table and list elements
    #


    def td(self, content, **attrs):
        """ Only for text content. You can't put HTML structure here. """
        self.write_html(self._render_content_tag('td', content, **attrs))


    def li(self, content, **attrs):
        """ Only for text content. You can't put HTML structure here. """
        self.write_html(self._render_content_tag('li', content, **attrs))


    #
    # structural text elements
    #


    def render_heading(self, content):
        """ <h2>%(content)</h2> """
        return self._render_content_tag('h2', content)


    def heading(self, content):
        self.write_html(self.render_heading(content))


    def render_br(self):
        return HTML("<br/>")


    def br(self):
        self.write_html(self.render_br())


    def render_hr(self, **attrs):
        return self._render_opening_tag('hr', close_tag=True, **attrs)


    def hr(self, **attrs):
        self.write_html(self.render_hr(**attrs))


    def rule(self):
        return self.hr()


    def render_nbsp(self):
        return HTML("&nbsp;")


    def nbsp(self):
        self.write_html(self.render_nbsp())


#.
#   .--TimeoutMgr.---------------------------------------------------------.
#   |      _____ _                            _   __  __                   |
#   |     |_   _(_)_ __ ___   ___  ___  _   _| |_|  \/  | __ _ _ __        |
#   |       | | | | '_ ` _ \ / _ \/ _ \| | | | __| |\/| |/ _` | '__|       |
#   |       | | | | | | | | |  __/ (_) | |_| | |_| |  | | (_| | | _        |
#   |       |_| |_|_| |_| |_|\___|\___/ \__,_|\__|_|  |_|\__, |_|(_)       |
#   |                                                    |___/             |
#   '----------------------------------------------------------------------'

class TimeoutManager(object):
    """Request timeout handling

    The system apache process will end the communication with the client after
    the timeout configured for the proxy connection from system apache to site
    apache. This is done in /omd/sites/[site]/etc/apache/proxy-port.conf file
    in the "timeout=x" parameter of the ProxyPass statement.

    The regular request timeout configured here should always be lower to make
    it possible to abort the page processing and send a helpful answer to the
    client.

    It is possible to disable the applications request timeout (temoporarily)
    or totally for specific calls, but the timeout to the client will always
    be applied by the system webserver. So the client will always get a error
    page while the site apache continues processing the request (until the
    first try to write anything to the client) which will result in an
    exception.
    """

    def enable_timeout(self, duration):
        def handle_request_timeout(signum, frame):
            raise RequestTimeout(_("Your request timed out after %d seconds. This issue may be "
                                   "related to a local configuration problem or a request which works "
                                   "with a too large number of objects. But if you think this "
                                   "issue is a bug, please send a crash report.") % duration)


        signal.signal(signal.SIGALRM, handle_request_timeout)
        signal.alarm(duration)


    def disable_timeout(self):
        signal.alarm(0)


#.
#   .--Transactions--------------------------------------------------------.
#   |      _____                               _   _                       |
#   |     |_   _| __ __ _ _ __  ___  __ _  ___| |_(_) ___  _ __  ___       |
#   |       | || '__/ _` | '_ \/ __|/ _` |/ __| __| |/ _ \| '_ \/ __|      |
#   |       | || | | (_| | | | \__ \ (_| | (__| |_| | (_) | | | \__ \      |
#   |       |_||_|  \__,_|_| |_|___/\__,_|\___|\__|_|\___/|_| |_|___/      |
#   |                                                                      |
#   '----------------------------------------------------------------------'

class TransactionManager(object):
    """Manages the handling of transaction IDs used by the GUI to prevent against
    performing the same action multiple times."""

    def __init__(self, request):
        super(TransactionManager, self).__init__()
        self._request = request

        self._new_transids    = []
        self._ignore_transids = False
        self._current_transid = None


    def ignore(self):
        """Makes the GUI skip all transaction validation steps"""
        self._ignore_transids = True


    def get(self):
        """Returns a transaction ID that can be used during a subsequent action"""
        if not self._current_transid:
            self._current_transid = self._fresh_transid()
        return self._current_transid


    def _fresh_transid(self):
        """Compute a (hopefully) unique transaction id.

        This is generated during rendering of a form or an action link, stored
        in a user specific file for later validation, sent to the users browser
        via HTML code, then submitted by the user together with the action
        (link / form) and then validated if it is a known transid. When it is a
        known transid, it will be used and invalidated. If the id is not known,
        the action will not be processed."""
        transid = "%d/%d" % (int(time.time()), random.getrandbits(32))
        self._new_transids.append(transid)
        return transid


    def store_new(self):
        """All generated transids are saved per user.

        They are stored in the transids.mk.  Per user only up to 20 transids of
        the already existing ones are kept. The transids generated on the
        current page are all kept. IDs older than one day are deleted."""
        if not self._new_transids:
            return

        valid_ids = self._load_transids(lock = True)
        cleared_ids = []
        now = time.time()
        for valid_id in valid_ids:
            timestamp = valid_id.split("/")[0]
            if now - int(timestamp) < 86400: # one day
                cleared_ids.append(valid_id)
        self._save_transids((cleared_ids[-20:] + self._new_transids))


    def transaction_valid(self):
        """Checks if the current transaction is valid

        i.e. in case of browser reload a browser reload, the form submit should
        not be handled  a second time.. The HTML variable _transid must be
        present.

        In case of automation users (authed by _secret in URL): If it is empty
        or -1, then it's always valid (this is used for webservice calls).
        This was also possible for normal users, but has been removed to preven
        security related issues."""
        if not self._request.has_var("_transid"):
            return False

        id = self._request.var("_transid")
        if self._ignore_transids and (not id or id == '-1'):
            return True # automation

        if '/' not in id:
            return False

        # Normal user/password auth user handling
        timestamp = id.split("/", 1)[0]

        # If age is too old (one week), it is always
        # invalid:
        now = time.time()
        if now - int(timestamp) >= 604800: # 7 * 24 hours
            return False

        # Now check, if this id is a valid one
        return id in self._load_transids()


    def is_transaction(self):
        """Checks, if the current page is a transation, i.e. something that is secured by
        a transid (such as a submitted form)"""
        return self._request.has_var("_transid")


    def check_transaction(self):
        """called by page functions in order to check, if this was a reload or the original form submission.

        Increases the transid of the user, if the latter was the case.

        There are three return codes:

        True:  -> positive confirmation by the user
        False: -> not yet confirmed, question is being shown
        None:  -> a browser reload or a negative confirmation
        """
        if self.transaction_valid():
            id = self._request.var("_transid")
            if id and id != "-1":
                self._invalidate(id)
            return True
        else:
            return False


    def _invalidate(self, used_id):
        """Remove the used transid from the list of valid ones"""
        valid_ids = self._load_transids(lock = True)
        try:
            valid_ids.remove(used_id)
        except ValueError:
            return
        self._save_transids(valid_ids)


    def _load_transids(self, lock = False):
        return config.user.load_file("transids", [], lock)


    def _save_transids(self, used_ids):
        if config.user.id:
            config.user.save_file("transids", used_ids)


#.
#   .--html----------------------------------------------------------------.
#   |                        _     _             _                         |
#   |                       | |__ | |_ _ __ ___ | |                        |
#   |                       | '_ \| __| '_ ` _ \| |                        |
#   |                       | | | | |_| | | | | | |                        |
#   |                       |_| |_|\__|_| |_| |_|_|                        |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Caution! The class needs to be derived from Outputfunnel first!      |
#   '----------------------------------------------------------------------'


class html(HTMLGenerator):

    def __init__(self, request, response):
        super(html, self).__init__()

        self._logger = log.logger.getChild("html")

        # rendering state
        self._header_sent = False
        self._context_buttons_open = False

        # style options
        self._body_classes = ['main']
        self._default_stylesheets = [ "check_mk", "graphs" ]
        self._default_javascripts = [ "checkmk", "graphs" ]

        # behaviour options
        self.render_headfoot = True
        self.enable_debug = False
        self.screenshotmode = False
        self.have_help = False
        self.help_visible = False

        # browser options
        self.output_format = "html"
        self.browser_reload = 0
        self.browser_redirect = ''
        self.link_target = None
        self.myfile = None

        # Browser options
        self._user_id = None
        self.user_errors = {}
        self.focus_object = None
        self.events = set([]) # currently used only for sounds
        self.status_icons = {}
        self.final_javascript_code = ""
        self.caches = {}
        self.treestates = None
        self.page_context = {}

        # Settings
        self.mobile = False

        # Forms
        self.form_name = None
        self.form_vars = []

        # Time measurement
        self.times            = {}
        self.start_time       = time.time()
        self.last_measurement = self.start_time

        # Variable management
        self._var_stash = []

        # Register helpers
        self.encoder = Encoder()
        self.timeout_manager = TimeoutManager()
        self.transaction_manager = TransactionManager(request)
        self.request = request
        self.response = response

        self.enable_request_timeout()

        self.response.set_content_type("text/html; charset=UTF-8")

        self.init_mobile()

        self.myfile = self._requested_file_name()

        # Disable caching for all our pages as they are mostly dynamically generated,
        # user related and are required to be up-to-date on every refresh
        self.response.set_http_header("Cache-Control", "no-cache")

        self.set_output_format(self.var("output_format", "html").lower())


    # TODO: Refactor call sites
    def _lowlevel_write(self, text):
        self.response.write(text)


    def init_modes(self):
        """Initializes the operation mode of the html() object. This is called
        after the Check_MK GUI configuration has been loaded, so it is safe
        to rely on the config."""
        self._verify_not_using_threaded_mpm()

        self._init_screenshot_mode()
        self._init_debug_mode()


    def _verify_not_using_threaded_mpm(self):
        if self.request.is_multithreaded:
            raise MKGeneralException(
                _("You are trying to Check_MK together with a threaded Apache multiprocessing module (MPM). "
                  "Check_MK is only working with the prefork module. Please change the MPM module to make "
                  "Check_MK work."))


    def _init_debug_mode(self):
        # Debug flag may be set via URL to override the configuration
        if self.var("debug"):
            config.debug = True
        self.enable_debug = config.debug


    # Enabling the screenshot mode omits the fancy background and
    # makes it white instead.
    def _init_screenshot_mode(self):
        if self.var("screenshotmode", config.screenshotmode):
            self.screenshotmode = True


    def _requested_file_name(self):
        parts = self.request.requested_file.rstrip("/").split("/")

        if len(parts) == 3 and parts[-1] == "check_mk":
            # Load index page when accessing /[site]/check_mk
            myfile = "index"

        elif parts[-1].endswith(".py"):
            # Regular pages end with .py - Stript it away to get the page name
            myfile = parts[-1][:-3]
            if myfile == "":
                myfile = "index"

        else:
            myfile = "index"

        # Redirect to mobile GUI if we are a mobile device and the index is requested
        if myfile == "index" and self.mobile:
            self.myfile = "mobile"

        return myfile


    def init_mobile(self):
        if self.has_var("mobile"):
            # TODO: Make private
            self.mobile = bool(self.var("mobile"))
            # Persist the explicitly set state in a cookie to have it maintained through further requests
            self.response.set_cookie("mobile", str(int(self.mobile)))

        elif self.request.has_cookie("mobile"):
            self.mobile = self.cookie("mobile", "0") == "1"

        else:
            self.mobile = self._is_mobile_client(self.request.user_agent)

    def _is_mobile_client(self, user_agent):
        # These regexes are taken from the public domain code of Matt Sullivan
        # http://sullerton.com/2011/03/django-mobile-browser-detection-middleware/
        reg_b = re.compile(r"android.+mobile|avantgo|bada\\/|blackberry|bb10|blazer|compal|elaine|fennec|hiptop|iemobile|ip(hone|od)|iris|kindle|lge |maemo|midp|mmp|opera m(ob|in)i|palm( os)?|phone|p(ixi|re)\\/|plucker|pocket|psp|symbian|treo|up\\.(browser|link)|vodafone|wap|windows (ce|phone)|xda|xiino", re.I|re.M)
        reg_v = re.compile(r"1207|6310|6590|3gso|4thp|50[1-6]i|770s|802s|a wa|abac|ac(er|oo|s\\-)|ai(ko|rn)|al(av|ca|co)|amoi|an(ex|ny|yw)|aptu|ar(ch|go)|as(te|us)|attw|au(di|\\-m|r |s )|avan|be(ck|ll|nq)|bi(lb|rd)|bl(ac|az)|br(e|v)w|bumb|bw\\-(n|u)|c55\\/|capi|ccwa|cdm\\-|cell|chtm|cldc|cmd\\-|co(mp|nd)|craw|da(it|ll|ng)|dbte|dc\\-s|devi|dica|dmob|do(c|p)o|ds(12|\\-d)|el(49|ai)|em(l2|ul)|er(ic|k0)|esl8|ez([4-7]0|os|wa|ze)|fetc|fly(\\-|_)|g1 u|g560|gene|gf\\-5|g\\-mo|go(\\.w|od)|gr(ad|un)|haie|hcit|hd\\-(m|p|t)|hei\\-|hi(pt|ta)|hp( i|ip)|hs\\-c|ht(c(\\-| |_|a|g|p|s|t)|tp)|hu(aw|tc)|i\\-(20|go|ma)|i230|iac( |\\-|\\/)|ibro|idea|ig01|ikom|im1k|inno|ipaq|iris|ja(t|v)a|jbro|jemu|jigs|kddi|keji|kgt( |\\/)|klon|kpt |kwc\\-|kyo(c|k)|le(no|xi)|lg( g|\\/(k|l|u)|50|54|e\\-|e\\/|\\-[a-w])|libw|lynx|m1\\-w|m3ga|m50\\/|ma(te|ui|xo)|mc(01|21|ca)|m\\-cr|me(di|rc|ri)|mi(o8|oa|ts)|mmef|mo(01|02|bi|de|do|t(\\-| |o|v)|zz)|mt(50|p1|v )|mwbp|mywa|n10[0-2]|n20[2-3]|n30(0|2)|n50(0|2|5)|n7(0(0|1)|10)|ne((c|m)\\-|on|tf|wf|wg|wt)|nok(6|i)|nzph|o2im|op(ti|wv)|oran|owg1|p800|pan(a|d|t)|pdxg|pg(13|\\-([1-8]|c))|phil|pire|pl(ay|uc)|pn\\-2|po(ck|rt|se)|prox|psio|pt\\-g|qa\\-a|qc(07|12|21|32|60|\\-[2-7]|i\\-)|qtek|r380|r600|raks|rim9|ro(ve|zo)|s55\\/|sa(ge|ma|mm|ms|ny|va)|sc(01|h\\-|oo|p\\-)|sdk\\/|se(c(\\-|0|1)|47|mc|nd|ri)|sgh\\-|shar|sie(\\-|m)|sk\\-0|sl(45|id)|sm(al|ar|b3|it|t5)|so(ft|ny)|sp(01|h\\-|v\\-|v )|sy(01|mb)|t2(18|50)|t6(00|10|18)|ta(gt|lk)|tcl\\-|tdg\\-|tel(i|m)|tim\\-|t\\-mo|to(pl|sh)|ts(70|m\\-|m3|m5)|tx\\-9|up(\\.b|g1|si)|utst|v400|v750|veri|vi(rg|te)|vk(40|5[0-3]|\\-v)|vm40|voda|vulc|vx(52|53|60|61|70|80|81|83|85|98)|w3c(\\-| )|webc|whit|wi(g |nc|nw)|wmlb|wonu|x700|xda(\\-|2|g)|yas\\-|your|zeto|zte\\-", re.I|re.M)

        return reg_b.search(user_agent) or reg_v.search(user_agent[0:4])

    #
    # HTTP variable processing
    #

    # TODO: Refactor call sites to html.request.*
    def var(self, varname, deflt=None):
        return self.request.var(varname, deflt)


    # TODO: Refactor call sites to html.request.*
    def has_var(self, varname):
        return self.request.var(varname)


    # Checks if a variable with a given prefix is present
    # TODO: Refactor call sites to html.request.*
    def has_var_prefix(self, prefix):
        return self.request.has_var_prefix(prefix)


    # TODO: Refactor call sites to html.request.*
    def var_utf8(self, varname, deflt = None):
        return self.request.var_utf8(varname, deflt)


    # TODO: Refactor call sites to html.request.*
    def all_vars(self):
        return self.request.all_vars()


    # TODO: Refactor call sites to html.request.*
    def all_varnames_with_prefix(self, prefix):
        return self.request.all_varnames_with_prefix(prefix)


    # Return all values of a variable that possible occurs more
    # than once in the URL. note: self.listvars does contain those
    # variable only, if the really occur more than once.
    # TODO: Refactor call sites to html.request.*
    def list_var(self, varname):
        return self.request.list_var(varname)


    # Adds a variable to listvars and also set it
    # TODO: Refactor call sites to html.request.*
    def add_var(self, varname, value):
        self.request.add_var(varname, value)


    # TODO: Refactor call sites to html.request.*
    def set_var(self, varname, value):
        self.request.set_var(varname, value)


    # TODO: Refactor call sites to html.request.*
    def del_var(self, varname):
        self.request.del_var(varname)


    # TODO: Refactor call sites to html.request.*
    def del_all_vars(self, prefix = None):
        self.request.del_all_vars(prefix)


    def stash_vars(self):
        self._var_stash.append(self.request.vars.copy())


    def unstash_vars(self):
        self.request.vars = self._var_stash.pop()


    def get_unicode_input(self, varname, deflt = None):
        try:
            return self.var_utf8(varname, deflt)
        except UnicodeDecodeError:
            raise MKUserError(varname, _("The given text is wrong encoded. "
                                         "You need to provide a UTF-8 encoded text."))

    def get_integer_input(self, varname, deflt=None):
        if deflt is not None and not self.has_var(varname):
            return deflt

        try:
            return int(self.var(varname))
        except TypeError:
            raise MKUserError(varname, _("The parameter \"%s\" is missing.") % varname)
        except ValueError:
            raise MKUserError(varname, _("The parameter \"%s\" is not an integer.") % varname)


    # Returns a dictionary containing all parameters the user handed over to this request.
    # The concept is that the user can either provide the data in a single "request" variable,
    # which contains the request data encoded as JSON, or provide multiple GET/POST vars which
    # are then used as top level entries in the request object.
    def get_request(self, exclude_vars=None):
        if exclude_vars == None:
            exclude_vars = []

        if self.var("request_format") == "python":
            try:
                python_request = self.var("request", "{}")
                request = ast.literal_eval(python_request)
            except (SyntaxError, ValueError) as e:
                raise MKUserError("request", _("Failed to parse Python request: '%s': %s") %
                                                                    (python_request, e))
        else:
            try:
                json_request = self.var("request", "{}")
                request = json.loads(json_request)
                request["request_format"] = "json"
            except ValueError, e: # Python3: json.JSONDecodeError
                raise MKUserError("request", _("Failed to parse JSON request: '%s': %s") %
                                                                    (json_request, e))


        for key, val in self.all_vars().items():
            if key not in [ "request", "output_format" ] + exclude_vars:
                request[key] = val.decode("utf-8")

        return request

    #
    # Transaction IDs
    #

    # TODO: Cleanup all call sites to self.transaction_manager.*
    def transaction_valid(self):
        return self.transaction_manager.transaction_valid()


    # TODO: Cleanup all call sites to self.transaction_manager.*
    def is_transaction(self):
        return self.transaction_manager.is_transaction()


    # TODO: Cleanup all call sites to self.transaction_manager.*
    def check_transaction(self):
        return self.transaction_manager.check_transaction()


    #
    # Encoding
    #

    # TODO: Cleanup all call sites to self.encoder.*
    def urlencode_vars(self, vars):
        return self.encoder.urlencode_vars(vars)

    # TODO: Cleanup all call sites to self.encoder.*
    def urlencode(self, value):
        return self.encoder.urlencode(value)

    #
    # escaping - deprecated functions
    #
    # Encode HTML attributes: e.g. replace '"' with '&quot;', '<' and '>' with '&lt;' and '&gt;'
    # TODO: Cleanup all call sites to self.escaper.*
    def attrencode(self, value):
        return self.escaper.escape_attribute(value)

    # Only strip off some tags. We allow some simple tags like <b> or <tt>.
    # TODO: Cleanup all call sites to self.escaper.*
    def permissive_attrencode(self, obj):
        return self.escaper.escape_text(obj)


    #
    # Stripping
    #

    # remove all HTML-tags
    def strip_tags(self, ht):

        if isinstance(ht, HTML):
            ht = "%s" % ht

        if type(ht) not in [str, unicode]:
            return ht

        while True:
            x = ht.find('<')
            if x == -1:
                break
            y = ht.find('>', x)
            if y == -1:
                break
            ht = ht[0:x] + ht[y+1:]
        return ht.replace("&nbsp;", " ")


    def strip_scripts(self, ht):
        while True:
            x = ht.find('<script')
            if x == -1:
                break
            y = ht.find('</script>')
            if y == -1:
                break
            ht = ht[0:x] + ht[y+9:]
        return ht


    #
    # Timeout handling
    #

    def enable_request_timeout(self):
        self.timeout_manager.enable_timeout(self.request.request_timeout)


    def disable_request_timeout(self):
        self.timeout_manager.disable_timeout()


    #
    # Content Type
    #

    def set_output_format(self, f):
        if f == "json":
            content_type = "application/json; charset=UTF-8"

        elif f == "jsonp":
            content_type = "application/javascript; charset=UTF-8"

        elif f in ("csv", "csv_export"): # Cleanup: drop one of these
            content_type = "text/csv; charset=UTF-8"

        elif f == "python":
            content_type = "text/plain; charset=UTF-8"

        elif f == "text":
            content_type = "text/plain; charset=UTF-8"

        elif f == "html":
            content_type = "text/html; charset=UTF-8"

        elif f == "xml":
            content_type = "text/xml; charset=UTF-8"

        elif f == "pdf":
            content_type = "application/pdf"

        else:
            raise MKGeneralException(_("Unsupported context type '%s'") % f)

        self.output_format = f
        self.response.set_content_type(content_type)


    def is_api_call(self):
        return self.output_format != "html"


    #
    # Other things
    #

    def measure_time(self, name):
        self.times.setdefault(name, 0.0)
        now = time.time()
        elapsed = now - self.last_measurement
        self.times[name] += elapsed
        self.last_measurement = now


    def set_user_id(self, user_id):
        self._user_id = user_id
        # TODO: Shouldn't this be moved to some other place?
        self.help_visible = config.user.load_file("help", False)  # cache for later usage


    def is_mobile(self):
        return self.mobile


    def set_page_context(self, c):
        self.page_context = c


    def set_link_target(self, framename):
        self.link_target = framename


    def set_focus(self, varname):
        self.focus_object = (self.form_name, varname)


    def set_focus_by_id(self, dom_id):
        self.focus_object = dom_id


    def set_render_headfoot(self, render):
        self.render_headfoot = render


    def set_browser_reload(self, secs):
        self.browser_reload = secs


    def set_browser_redirect(self, secs, url):
        self.browser_reload   = secs
        self.browser_redirect = url


    def add_default_stylesheet(self, name):
        if name not in self._default_stylesheets:
            self._default_stylesheets.append(name)


    def add_default_javascript(self, name):
        if name not in self._default_javascripts:
            self._default_javascripts.append(name)


    def immediate_browser_redirect(self, secs, url):
        self.javascript("set_reload(%s, '%s');" % (secs, url))


    def add_body_css_class(self, cls):
        self._body_classes.append(cls)


    def add_status_icon(self, img, tooltip, url = None):
        if url:
            self.status_icons[img] = tooltip, url
        else:
            self.status_icons[img] = tooltip


    def final_javascript(self, code):
        self.final_javascript_code += code + "\n"


    def reload_sidebar(self):
        if not self.has_var("_ajaxid"):
            self.write_html(self.render_reload_sidebar())


    def render_reload_sidebar(self):
        return self.render_javascript("reload_sidebar()")


    #
    # Tree states
    #

    def get_tree_states(self, tree):
        self.load_tree_states()
        return self.treestates.get(tree, {})


    def set_tree_state(self, tree, key, val):
        self.load_tree_states()

        if tree not in self.treestates:
            self.treestates[tree] = {}

        self.treestates[tree][key] = val


    def set_tree_states(self, tree, val):
        self.load_tree_states()
        self.treestates[tree] = val


    def save_tree_states(self):
        config.user.save_file("treestates", self.treestates)


    def load_tree_states(self):
        if self.treestates == None:
            self.treestates = config.user.load_file("treestates", {})


    def finalize(self):
        """Finish the HTTP request processing before handing over to the application server"""
        self.transaction_manager.store_new()
        self.disable_request_timeout()


    #
    # Messages
    #


    def show_info(self, msg):
        self.message(msg, 'message')


    def show_error(self, msg):
        self.message(msg, 'error')


    def show_warning(self, msg):
        self.message(msg, 'warning')


    def render_info(self, msg):
        return self.render_message(msg, 'message')


    def render_error(self, msg):
        return self.render_message(msg, 'error')


    def render_warning(self, msg):
        return self.render_message(msg, 'warning')


    def message(self, msg, what='message'):
        self.write(self.render_message(msg, what))


    # obj might be either a string (str or unicode) or an exception object
    def render_message(self, msg, what='message'):
        if what == 'message':
            cls    = 'success'
            prefix = _('MESSAGE')
        elif what == 'warning':
            cls    = 'warning'
            prefix = _('WARNING')
        else:
            cls    = 'error'
            prefix = _('ERROR')

        code = ""

        if self.output_format == "html":
            code += self.render_div(self.render_text(msg), class_=cls)
            if self.mobile:
                code += self.render_center(code)
        else:
            code += self.render_text('%s: %s\n' % (prefix, self.strip_tags(msg)))

        return code


    def show_localization_hint(self):
        url = "wato.py?mode=edit_configvar&varname=user_localizations"
        self.message(self.render_sup("*") +
                     _("These texts may be localized depending on the users' "
                       "language. You can configure the localizations %s.")
                        % self.render_a("in the global settings", href=url))


    def help(self, text):
        self.write_html(self.render_help(text))


    # Embed help box, whose visibility is controlled by a global button in the page.
    def render_help(self, text):
        if text and text.strip():
            self.have_help = True
            style = "display: %s;" % ("block" if self.help_visible else "none")
            c = self.render_div(text.strip(), class_="help", style=style)
            return c
        else:
            return ""


    #
    # Debugging, diagnose and logging
    #

    def debug(self, *x):
        for element in x:
            try:
                formatted = pprint.pformat(element)
            except UnicodeDecodeError:
                formatted = repr(element)
            self.write(self.render_pre(formatted))


    #
    # URL building
    #

    # [('varname1', value1), ('varname2', value2) ]
    def makeuri(self, addvars, remove_prefix=None, filename=None, delvars=None):
        new_vars = [ nv[0] for nv in addvars ]
        vars = [ (v, self.var(v))
                 for v in self.request.vars
                 if v[0] != "_" and v not in new_vars and (not delvars or v not in delvars) ]
        if remove_prefix != None:
            vars = [ i for i in vars if not i[0].startswith(remove_prefix) ]
        vars = vars + addvars
        if filename == None:
            filename = self.urlencode(self.myfile) + ".py"
        if vars:
            return filename + "?" + self.urlencode_vars(vars)
        else:
            return filename


    def makeuri_contextless(self, vars, filename=None):
        if not filename:
            filename = self.myfile + ".py"
        if vars:
            return filename + "?" + self.urlencode_vars(vars)
        else:
            return filename


    def makeactionuri(self, addvars, filename=None, delvars=None):
        return self.makeuri(addvars + [("_transid", self.transaction_manager.get())],
                            filename=filename, delvars=delvars)


    def makeactionuri_contextless(self, addvars, filename=None):
        return self.makeuri_contextless(addvars + [("_transid", self.transaction_manager.get())],
                                        filename=filename)


    #
    # HTML heading and footer rendering
    #


    def default_html_headers(self):
        self.meta(httpequiv="Content-Type", content="text/html; charset=utf-8")
        self.meta(httpequiv="X-UA-Compatible", content="IE=edge")
        self.write_html(self._render_opening_tag('link', rel="shortcut icon", href="images/favicon.ico", type_="image/ico", close_tag=True))


    def _head(self, title, javascripts=None, stylesheets=None):

        javascripts = javascripts if javascripts else []
        stylesheets = stylesheets if stylesheets else ["pages"]

        self.open_head()

        self.default_html_headers()
        self.title(title)

        # If the variable _link_target is set, then all links in this page
        # should be targetted to the HTML frame named by _link_target. This
        # is e.g. useful in the dash-board
        if self.link_target:
            self.base(target=self.link_target)

        # Load all specified style sheets and all user style sheets in htdocs/css
        for css in self._default_stylesheets + stylesheets:
            fname = self._css_filename_for_browser(css)
            if fname is not None:
                self.stylesheet(fname)

        # write css for internet explorer
        fname = self._css_filename_for_browser("ie")
        if fname is not None:
            self.write_html("<!--[if IE]>\n")
            self.stylesheet(fname)
            self.write_html("<![endif]-->\n")

        self._add_custom_style_sheet()

        # Load all scripts
        for js in self._default_javascripts + javascripts:
            filename_for_browser = self.javascript_filename_for_browser(js)
            if filename_for_browser:
                self.javascript_file(filename_for_browser)

        if self.browser_reload != 0:
            if self.browser_redirect != '':
                self.javascript('set_reload(%s, \'%s\')' % (self.browser_reload, self.browser_redirect))
            else:
                self.javascript('set_reload(%s)' % (self.browser_reload))

        self.close_head()


    def _add_custom_style_sheet(self):
        for css in self._plugin_stylesheets():
            self.write('<link rel="stylesheet" type="text/css" href="css/%s">\n' % css)

        if config.custom_style_sheet:
            self.write('<link rel="stylesheet" type="text/css" href="%s">\n' % config.custom_style_sheet)

        if cmk.is_managed_edition():
            import cmk.gui.cme.gui_colors as gui_colors
            gui_colors.GUIColors().render_html()


    def _plugin_stylesheets(self):
        plugin_stylesheets = set([])
        for dir in [ cmk.paths.web_dir + "/htdocs/css", cmk.paths.local_web_dir + "/htdocs/css" ]:
            if os.path.exists(dir):
                for fn in os.listdir(dir):
                    if fn.endswith(".css"):
                        plugin_stylesheets.add(fn)
        return plugin_stylesheets


    # Make the browser load specified javascript files. We have some special handling here:
    # a) files which can not be found shal not be loaded
    # b) in OMD environments, add the Check_MK version to the version (prevents update problems)
    # c) load the minified javascript when not in debug mode
    def javascript_filename_for_browser(self, jsname):
        filename_for_browser = None
        rel_path = "/share/check_mk/web/htdocs/js"
        if self.enable_debug:
            min_parts = [ "", "_min" ]
        else:
            min_parts = [ "_min", "" ]

        for min_part in min_parts:
            path_pattern = cmk.paths.omd_root + "%s" + rel_path + "/" + jsname + min_part + ".js"
            if os.path.exists(path_pattern % "") or os.path.exists(path_pattern % "/local"):
                filename_for_browser = 'js/%s%s-%s.js' % (jsname, min_part, cmk.__version__)
                break

        return filename_for_browser


    def _css_filename_for_browser(self, css):
        rel_path = "/share/check_mk/web/htdocs/" + css + ".css"
        if os.path.exists(cmk.paths.omd_root + rel_path) or \
            os.path.exists(cmk.paths.omd_root + "/local" + rel_path):
            return '%s-%s.css' % (css, cmk.__version__)


    def html_head(self, title, javascripts=None, stylesheets=None, force=False):

        force_new_document = force # for backward stability and better readability

        if force_new_document:
            self._header_sent = False

        if not self._header_sent:
            self.write_html('<!DOCTYPE HTML>\n')
            self.open_html()
            self._head(title, javascripts, stylesheets)
            self._header_sent = True



    def header(self, title='', javascripts=None, stylesheets=None, force=False,
               show_body_start=True, show_top_heading=True):
        if self.output_format == "html":
            if not self._header_sent:
                if show_body_start:
                    self.body_start(title, javascripts=javascripts, stylesheets=stylesheets, force=force)

                self._header_sent = True

                if self.render_headfoot and show_top_heading:
                    self.top_heading(title)


    def body_start(self, title='', javascripts=None, stylesheets=None, force=False):
        self.html_head(title, javascripts, stylesheets, force)
        self.open_body(class_=self._get_body_css_classes())


    def _get_body_css_classes(self):
        if self.screenshotmode:
            return self._body_classes + ["screenshotmode"]
        else:
            return self._body_classes


    def html_foot(self):
        self.close_html()


    def top_heading(self, title):
        if not isinstance(config.user, config.LoggedInNobody):
            login_text = "<b>%s</b> (%s" % (config.user.id, "+".join(config.user.role_ids))
            if self.enable_debug:
                if config.user.language():
                    login_text += "/%s" % config.user.language()
            login_text += ')'
        else:
            login_text = _("not logged in")
        self.top_heading_left(title)

        self.write('<td style="min-width:240px" class=right><span id=headinfo></span>%s &nbsp; ' % login_text)
        if config.pagetitle_date_format:
            self.write(' &nbsp; <b id=headerdate format="%s"></b>' % config.pagetitle_date_format)
        self.write(' <b id=headertime></b>')
        self.javascript('update_header_timer()')
        self.top_heading_right()


    def top_heading_left(self, title):
        self.open_table(class_="header")
        self.open_tr()
        self.open_td(width="*", class_="heading")
        self.a(title, href="#", onfocus="if (this.blur) this.blur();",
               onclick="this.innerHTML=\'%s\'; document.location.reload();" % _("Reloading..."))
        self.close_td()


    def top_heading_right(self):
        cssclass = "active" if self.help_visible else "passive"

        self.icon_button(None, _("Toggle context help texts"), "help", id="helpbutton",
                         onclick="toggle_help()", style="display:none", ty="icon", cssclass=cssclass)
        self.open_a(href="https://mathias-kettner.com", class_="head_logo")
        self.img(src="images/logo_cmk_small.png")
        self.close_a()
        self.close_td()
        self.close_tr()
        self.close_table()
        self.hr(class_="header")

        if self.enable_debug:
            self._dump_get_vars()


    def footer(self, show_footer=True, show_body_end=True):
        if self.output_format == "html":
            if show_footer:
                self.bottom_footer()

            if show_body_end:
                self.body_end()


    def bottom_footer(self):
        if self._header_sent:
            self.bottom_focuscode()
            if self.render_headfoot:
                self.open_table(class_="footer")
                self.open_tr()

                self.open_td(class_="left")
                self._write_status_icons()
                self.close_td()

                self.td('', class_="middle")

                self.open_td(class_="right")
                content = _("refresh: %s secs") % self.render_div(self.browser_reload, id_="foot_refresh_time")
                style = "display:inline-block;" if self.browser_reload else "display:none;"
                self.div(HTML(content), id_="foot_refresh", style=style)
                self.close_td()

                self.close_tr()
                self.close_table()


    def bottom_focuscode(self):
        if self.focus_object:
            if type(self.focus_object) == tuple:
                formname, varname = self.focus_object
                obj_ident = formname + "." + varname
            else:
                obj_ident = "getElementById(\"%s\")" % self.focus_object

            js_code = "<!--\n" \
                      "var focus_obj = document.%s;\n" \
                      "if (focus_obj) {\n" \
                      "    focus_obj.focus();\n" \
                      "    if (focus_obj.select)\n" \
                      "        focus_obj.select();\n" \
                      "}\n" \
                      "// -->\n" % obj_ident
            self.javascript(js_code)


    def focus_here(self):
        self.a("", href="#focus_me", id_="focus_me")
        self.set_focus_by_id("focus_me")


    def body_end(self):
        if self.have_help:
            self.javascript("enable_help();")
        if self.final_javascript_code:
            self.javascript(self.final_javascript_code)
        self.javascript("initialize_visibility_detection();")
        self.close_body()
        self.close_html()


    #
    # HTML form rendering
    #

    def begin_form(self, name, action = None, method = "GET",
                   onsubmit = None, add_transid = True):
        self.form_vars = []
        if action == None:
            action = self.myfile + ".py"
        self.current_form = name
        self.open_form(id_="form_%s" % name, name=name, class_=name,
                       action=action, method=method, onsubmit=onsubmit,
                       enctype="multipart/form-data" if method.lower() == "post" else None)
        self.hidden_field("filled_in", name, add_var=True)
        if add_transid:
            self.hidden_field("_transid", str(self.transaction_manager.get()))
        self.form_name = name


    def end_form(self):
        self.close_form()
        self.form_name = None


    def in_form(self):
        return self.form_name != None


    def prevent_password_auto_completion(self):
        # These fields are not really used by the form. They are used to prevent the browsers
        # from filling the default password and previous input fields in the form
        # with password which are eventually saved in the browsers password store.
        self.input(name=None, type_="text", style="display:none;")
        self.input(name=None, type_="password", style="display:none;")


    # Needed if input elements are put into forms without the helper
    # functions of us. TODO: Should really be removed and cleaned up!
    def add_form_var(self, varname):
        self.form_vars.append(varname)


    # Beware: call this method just before end_form(). It will
    # add all current non-underscored HTML variables as hiddedn
    # field to the form - *if* they are not used in any input
    # field. (this is the reason why you must not add any further
    # input fields after this method has been called).
    def hidden_fields(self, varlist = None, **args):
        add_action_vars = args.get("add_action_vars", False)
        if varlist != None:
            for var in varlist:
                value = self.request.vars.get(var, "")
                self.hidden_field(var, value)
        else: # add *all* get variables, that are not set by any input!
            for var in self.request.vars:
                if var not in self.form_vars and \
                    (var[0] != "_" or add_action_vars): # and var != "filled_in":
                    self.hidden_field(var, self.get_unicode_input(var))


    def hidden_field(self, var, value, id=None, add_var=False, class_=None):
        self.write_html(self.render_hidden_field(var=var, value=value, id=id, add_var=add_var, class_=class_))


    def render_hidden_field(self, var, value, id=None, add_var=False, class_=None):
        # TODO: Refactor
        id_ = id
        if value == None:
            return ""
        if add_var:
            self.add_form_var(var)
        return self.render_input(name=var, type_="hidden", id_=id_, value=value, class_=class_)


    #
    # Form submission and variable handling
    #

    # Check if the current form is currently filled in (i.e. we display
    # the form a second time while showing value typed in at the first
    # time and complaining about invalid user input)
    def form_filled_in(self, form_name = None):
        if form_name == None:
            form_name = self.form_name

        return self.has_var("filled_in") and (
            form_name == None or \
            form_name in self.list_var("filled_in"))


    def do_actions(self):
        return self.var("_do_actions") not in [ "", None, _("No") ]


    def form_submitted(self, form_name=None):
        if form_name:
            return self.var("filled_in") == form_name
        else:
            return self.has_var("filled_in")


    # Get value of checkbox. Return True, False or None. None means
    # that no form has been submitted. The problem here is the distintion
    # between False and None. The browser does not set the variables for
    # Checkboxes that are not checked :-(
    def get_checkbox(self, varname, form_name = None):
        if self.has_var(varname):
            return not not self.var(varname)
        elif not self.form_filled_in(form_name):
            return None
        else:
            # Form filled in but variable missing -> Checkbox not checked
            return False


    #
    # Button elements
    #


    def button(self, varname, title, cssclass = None, style=None, help=None):
        self.write_html(self.render_button(varname, title, cssclass, style))


    def render_button(self, varname, title, cssclass = None, style=None, help=None):
        self.add_form_var(varname)
        return self.render_input(name=varname, type_="submit",
                                 id_=varname, class_=["button", cssclass if cssclass else None],
                                 value=title, title=help, style=style)


    def buttonlink(self, href, text, add_transid=False, obj_id=None, style=None, title=None, disabled=None):
        if add_transid:
            href += "&_transid=%s" % self.transaction_manager.get()
        if not obj_id:
            obj_id = utils.gen_id()
        self.input(name=obj_id, type_="button",
                   id_=obj_id, class_=["button", "buttonlink"],
                   value=text, style=style,
                   title=title, disabled=disabled,
                   onclick="location.href=\'%s\'" % href)


    # TODO: Refactor the arguments. It is only used in views/wato
    def toggle_button(self, id, isopen, icon, help, hidden=False, disabled=False, onclick=None, is_context_button=True):
        if is_context_button:
            self.begin_context_buttons() # TODO: Check all calls. If done before, remove this!

        if not onclick and not disabled:
            onclick = "view_toggle_form(this.parentNode, '%s');" % id

        if disabled:
            state    = "off" if disabled else "on"
            cssclass = ""
            help     = ""
        else:
            state = "on"
            if isopen:
                cssclass = "down"
            else:
                cssclass = "up"

        self.open_div(
            id_="%s_%s" % (id, state),
            class_=["togglebutton", state, icon, cssclass],
            title=help,
            style='display:none' if hidden else None,
        )
        self.open_a("javascript:void(0)", onclick=onclick)
        self.img(src="images/icon_%s.png" % icon)
        self.close_a()
        self.close_div()


    def get_button_counts(self):
        return config.user.load_file("buttoncounts", {})


    def empty_icon_button(self):
        self.write(self.render_icon("images/trans.png", cssclass="iconbutton trans"))


    def disabled_icon_button(self, icon):
        self.write(self.render_icon(icon, cssclass="iconbutton"))


    # TODO: Cleanup to use standard attributes etc.
    def jsbutton(self, varname, text, onclick, style='', cssclass=""):
        # autocomplete="off": Is needed for firefox not to set "disabled="disabled" during page reload
        # when it has been set on a page via javascript before. Needed for WATO activate changes page.
        self.input(name=varname, type_="button", id_=varname, class_=["button", cssclass],
                   autocomplete="off", onclick=onclick, style=style, value=text)


    #
    # Other input elements
    #


    def user_error(self, e):
        assert isinstance(e, MKUserError), "ERROR: This exception is not a user error!"
        self.open_div(class_="error")
        self.write("%s" % e.message)
        self.close_div()
        self.add_user_error(e.varname, e)


    # user errors are used by input elements to show invalid input
    def add_user_error(self, varname, msg_or_exc):
        if isinstance(msg_or_exc, Exception):
            message = "%s" % msg_or_exc
        else:
            message = msg_or_exc

        if type(varname) == list:
            for v in varname:
                self.add_user_error(v, message)
        else:
            self.user_errors[varname] = message


    def has_user_errors(self):
        return len(self.user_errors) > 0


    def show_user_errors(self):
        if self.has_user_errors():
            self.open_div(class_="error")
            self.write('<br>'.join(self.user_errors.values()))
            self.close_div()


    def text_input(self, varname, default_value = "", cssclass = "text", label = None, id_ = None,
                   submit = None, attrs = None, **args):
        if attrs is None:
            attrs = {}

        # Model
        error = self.user_errors.get(varname)
        value = self.request.vars.get(varname, default_value)
        if not value:
            value = ""
        if error:
            self.set_focus(varname)
        self.form_vars.append(varname)

        # View
        style, size = None, None
        if args.get("try_max_width"):
            style = "width: calc(100% - 10px); "
            if "size" in args:
                cols = int(args["size"])
            else:
                cols = 16
            style += "min-width: %d.8ex; " % cols

        elif "size" in args and args["size"]:
            if args["size"] == "max":
                style = "width: 100%;"
            else:
                size = "%d" % (args["size"] + 1)
                if not args.get('omit_css_width', False) and "width:" not in args.get("style", "") and not self.mobile:
                    style = "width: %d.8ex;" % args["size"]

        if args.get("style"):
            style = [style, args["style"]]

        if (submit or label) and not id_:
            id_ = "ti_%s" % varname

        onkeydown = None if not submit else HTML('textinput_enter_submit(\'%s\');' % submit)

        attributes = {"class"        : cssclass,
                      "id"           : id_,
                      "style"        : style,
                      "size"         : size,
                      "autocomplete" : args.get("autocomplete"),
                      "readonly"     : "true" if args.get("read_only") else None,
                      "value"        : value,
                      "onkeydown"    : onkeydown, }

        for key, val in attrs.iteritems():
            if key not in attributes and key not in ["name", "type", "type_"]:
                attributes[key] = val
            elif key in attributes and attributes[key] is None:
                attributes[key] = val

        if error:
            self.open_x(class_="inputerror")

        if label:
            self.label(label, for_=id_)
        self.write_html(self.render_input(varname, type_=args.get("type", "text"), **attributes))

        if error:
            self.close_x()


    # Shows a colored badge with text (used on WATO activation page for the site status)
    def status_label(self, content, status, help, **attrs):
        self.status_label_button(content, status, help, onclick=None, **attrs)


    # Shows a colored button with text (used in site and customer status snapins)
    def status_label_button(self, content, status, help, onclick, **attrs):
        button_cls = "button" if onclick else None
        self.div(content, title=help, class_=[ "status_label", button_cls, status ],
                 onclick=onclick, **attrs)


    def toggle_switch(self, enabled, help, **attrs):
        # Same API as other elements: class_ can be a list or string/None
        if "class_" in attrs:
            if type(attrs["class_"]) != list:
                attrs["class_"] = [ attrs["class_"] ]
        else:
            attrs["class_"] = []

        attrs["class_"] += [ "toggle_switch", "on" if enabled else "off", ]

        link_attrs = {
            "href"    : attrs.pop("href", "javascript:void(0)"),
            "onclick" : attrs.pop("onclick", None),
        }

        self.open_div(**attrs)
        self.a(_("on") if enabled else _("off"), title=help, **link_attrs)
        self.close_div()


    def number_input(self, varname, deflt = "", size=8, style="", submit=None):
        if deflt != None:
            deflt = str(deflt)
        self.text_input(varname, deflt, "number", size=size, style=style, submit=submit)


    def password_input(self, varname, default_value = "", size=12, **args):
        self.text_input(varname, default_value, type="password", size = size, **args)


    def text_area(self, varname, deflt="", rows=4, cols=30, attrs=None, try_max_width=False):
        if attrs is None:
            attrs = {}

        # Model
        value = self.var(varname, deflt)
        error = self.user_errors.get(varname)

        self.form_vars.append(varname)
        if error:
            self.set_focus(varname)

        # View
        style = "width: %d.8ex;" % cols
        if try_max_width:
            style += "width: calc(100%% - 10px); min-width: %d.8ex;" % cols
        attrs["style"] = style
        attrs["rows"] = rows
        attrs["cols"] = cols
        attrs["name"] = varname

        if error:
            self.open_x(class_="inputerror")
        self.write_html(self._render_content_tag("textarea", value, **attrs))
        if error:
            self.close_x()


    # TODO: DEPRECATED!!
    def sorted_select(self, varname, choices, deflt='', onchange=None, attrs=None):
        if attrs is None:
            attrs = {}
        self.dropdown(varname, choices, deflt=deflt, onchange=onchange, sorted = True, **attrs)


    # TODO: DEPRECATED!!
    def select(self, varname, choices, deflt='', onchange=None, attrs=None):
        if attrs is None:
            attrs = {}
        self.dropdown(varname, choices, deflt=deflt, onchange=onchange, **attrs)


    # TODO: DEPRECATED!!
    def icon_select(self, varname, choices, deflt=''):
        self.icon_dropdown(varname, choices, deflt=deflt)


    # Choices is a list pairs of (key, title). They keys of the choices
    # and the default value must be of type None, str or unicode.
    def dropdown(self, varname, choices, deflt='', sorted='', **attrs):

        current = self.get_unicode_input(varname, deflt)
        error = self.user_errors.get(varname)
        if varname:
            self.form_vars.append(varname)
        attrs.setdefault('size', 1)

        chs = choices[:]
        if sorted:
            # Sort according to display texts, not keys
            chs.sort(key=lambda a: a[1].lower())

        if error:
            self.open_x(class_="inputerror")

        if "read_only" in attrs and attrs.pop("read_only"):
            attrs["disabled"] = "disabled"
            self.hidden_field(varname, current, add_var=False)

        self.open_select(name=varname, id_=varname, **attrs)
        for value, text in chs:
            # if both the default in choices and current was '' then selected depended on the order in choices
            selected = (value == current) or (not value and not current)
            self.option(text, value=value if value else "",
                              selected="" if selected else None)
        self.close_select()
        if error:
            self.close_x()


    def icon_dropdown(self, varname, choices, deflt=""):
        current = self.var(varname, deflt)
        if varname:
            self.form_vars.append(varname)

        self.open_select(class_="icon", name=varname, id_=varname, size="1")
        for value, text, icon in choices:
            # if both the default in choices and current was '' then selected depended on the order in choices
            selected = (value == current) or (not value and not current)
            self.option(text, value=value if value else "",
                              selected='' if selected else None,
                              style="background-image:url(images/icon_%s.png);" % icon)
        self.close_select()


    # Wrapper for DualListChoice
    def multi_select(self, varname, choices, deflt='', sorted='', **attrs):
        attrs["multiple"] = "multiple"
        self.dropdown(varname, choices, deflt=deflt, sorted=sorted, **attrs)


    def upload_file(self, varname):
        error = self.user_errors.get(varname)
        if error:
            self.open_x(class_="inputerror")
        self.input(name=varname, type_="file")
        if error:
            self.close_x()
        self.form_vars.append(varname)


    # The confirm dialog is normally not a dialog which need to be protected
    # by a transid itselfs. It is only a intermediate step to the real action
    # But there are use cases where the confirm dialog is used during rendering
    # a normal page, for example when deleting a dashlet from a dashboard. In
    # such cases, the transid must be added by the confirm dialog.
    # add_header: A title can be given to make the confirm method render the HTML
    #             header when showing the confirm message.
    def confirm(self, msg, method="POST", action=None, add_transid=False, add_header=False):
        if self.var("_do_actions") == _("No"):
            # User has pressed "No", now invalidate the unused transid
            self.check_transaction()
            return # None --> "No"

        if not self.has_var("_do_confirm"):
            if add_header != False:
                self.header(add_header)

            if self.mobile:
                self.open_center()
            self.open_div(class_="really")
            self.write_text(msg)
            # FIXME: When this confirms another form, use the form name from self.request.vars()
            self.begin_form("confirm", method=method, action=action, add_transid=add_transid)
            self.hidden_fields(add_action_vars = True)
            self.button("_do_confirm", _("Yes!"), "really")
            self.button("_do_actions", _("No"), "")
            self.end_form()
            self.close_div()
            if self.mobile:
                self.close_center()

            return False # False --> "Dialog shown, no answer yet"
        else:
            # Now check the transaction
            return self.check_transaction() and True or None # True: "Yes", None --> Browser reload of "yes" page


    #
    # Radio groups
    #


    def begin_radio_group(self, horizontal=False):
        if self.mobile:
            attrs = {'data-type' : "horizontal" if horizontal else None,
                     'data-role' : "controlgroup"}
            self.write(self._render_opening_tag("fieldset", **attrs))


    def end_radio_group(self):
        if self.mobile:
            self.write(self._render_closing_tag("fieldset"))


    def radiobutton(self, varname, value, checked, label):
        # Model
        self.form_vars.append(varname)

        # Controller
        if self.has_var(varname):
            checked = self.var(varname) == value

        # View
        id_="rb_%s_%s" % (varname, value) if label else None
        self.input(name=varname, type_="radio", value = value,
                   checked='' if checked else None, id_=id_)
        if label:
            self.label(label, for_=id_)


    #
    # Checkbox groups
    #


    def begin_checkbox_group(self, horizonal=False):
        self.begin_radio_group(horizonal)


    def end_checkbox_group(self):
        self.end_radio_group()


    def checkbox(self, *args, **kwargs):
        self.write(self.render_checkbox(*args, **kwargs))


    def render_checkbox(self, varname, deflt = False, label = '', id = None, **add_attr):

        # Problem with checkboxes: The browser will add the variable
        # only to the URL if the box is checked. So in order to detect
        # whether we should add the default value, we need to detect
        # if the form is printed for the first time. This is the
        # case if "filled_in" is not set.
        value = self.get_checkbox(varname)
        if value is None: # form not yet filled in
            value = deflt

        error = self.user_errors.get(varname)
        if id is None:
            id = "cb_%s" % varname

        add_attr["id"] = id
        add_attr["CHECKED"] = '' if value else None

        code = self.render_input(name=varname, type_="checkbox", **add_attr)\
             + self.render_label(label, for_=id)
        code = self.render_span(code, class_="checkbox")

        if error:
            code = self.render_x(code, class_="inputerror")

        self.form_vars.append(varname)
        return code


    #
    # Foldable context
    #


    def begin_foldable_container(self, treename, id, isopen, title, indent=True,
                                 first=False, icon=None, fetch_url=None, title_url=None,
                                 tree_img="tree"):
        self.folding_indent = indent

        if self._user_id:
            isopen = self.foldable_container_is_open(treename, id, isopen)

        onclick = "toggle_foldable_container(\'%s\', \'%s\', \'%s\')"\
                    % (treename, id, fetch_url if fetch_url else '')

        img_id = "treeimg.%s.%s" % (treename, id)

        if indent == "nform":
            self.open_tr(class_="heading")
            self.open_td(id_="nform.%s.%s" % (treename, id), onclick=onclick, colspan="2")
            if icon:
                self.img(class_=["treeangle", "title"], src="images/icon_%s.png" % icon)
            else:
                self.img(id_=img_id, class_=["treeangle", "nform", "open" if isopen else "closed"],
                         src="images/%s_closed.png" % tree_img, align="absbottom")
            self.write_text(title)
            self.close_td()
            self.close_tr()
        else:
            self.open_div(class_="foldable")

            if not icon:
                self.img(id_="treeimg.%s.%s" % (treename, id),
                         class_=["treeangle", "open" if isopen else "closed"],
                         src="images/%s_closed.png" % tree_img, align="absbottom", onclick=onclick)
            if isinstance(title, HTML): # custom HTML code
                if icon:
                    self.img(class_=["treeangle", "title"], src="images/icon_%s.png" % icon, onclick=onclick)
                self.write_text(title)
                if indent != "form":
                    self.br()
            else:
                self.open_b(class_=["treeangle", "title"], onclick=None if title_url else onclick)
                if icon:
                    self.img(class_=["treeangle", "title"], src="images/icon_%s.png" % icon)
                if title_url:
                    self.a(title, href=title_url)
                else:
                    self.write_text(title)
                self.close_b()
                self.br()

            indent_style = "padding-left: %dpx; " % (indent == True and 15 or 0)
            if indent == "form":
                self.close_td()
                self.close_tr()
                self.close_table()
                indent_style += "margin: 0; "
            self.open_ul(id_="tree.%s.%s" % (treename, id),
                         class_=["treeangle", "open" if isopen else "closed"], style=indent_style)

        # give caller information about current toggling state (needed for nform)
        return isopen


    def end_foldable_container(self):
        if self.folding_indent != "nform":
            self.close_ul()
            self.close_div()


    def foldable_container_is_open(self, treename, id, isopen):
        # try to get persisted state of tree
        tree_state = self.get_tree_states(treename)

        if id in tree_state:
            isopen = tree_state[id] == "on"
        return isopen


    #
    # Context Buttons
    #


    def begin_context_buttons(self):
        if not self._context_buttons_open:
            self.context_button_hidden = False
            self.open_div(class_="contextlinks")
            self._context_buttons_open = True


    def end_context_buttons(self):
        if self._context_buttons_open:
            if self.context_button_hidden:
                self.open_div(title=_("Show all buttons"), id="toggle", class_=["contextlink", "short"])
                self.a("...", onclick='unhide_context_buttons(this);', href='#')
                self.close_div()
            self.div("", class_="end")
            self.close_div()
        self._context_buttons_open = False


    def context_button(self, title, url, icon=None, hot=False, id=None, bestof=None, hover_title=None, class_=None):
        # TODO: REFACTOR
        id_ = id
        self._context_button(title, url, icon=icon, hot=hot, id_=id_, bestof=bestof, hover_title=hover_title, class_=class_)


    def _context_button(self, title, url, icon=None, hot=False, id_=None, bestof=None, hover_title=None, class_=None):
        title = self.attrencode(title)
        display = "block"
        if bestof:
            counts = self.get_button_counts()
            weights = counts.items()
            weights.sort(cmp = lambda a,b: cmp(a[1],  b[1]))
            best = dict(weights[-bestof:]) # pylint: disable=invalid-unary-operand-type
            if id_ not in best:
                display="none"
                self.context_button_hidden = True

        if not self._context_buttons_open:
            self.begin_context_buttons()

        css_classes = [ "contextlink" ]
        if hot:
            css_classes.append("hot")
        if class_:
            if type(class_) == list:
                css_classes += class_
            else:
                css_classes += class_.split(" ")

        self.open_div(class_=css_classes, id_=id_, style="display:%s;" % display)

        self.open_a(href=url, title=hover_title, onclick="count_context_button(this);" if bestof else None)

        if icon:
            self.icon('', icon, cssclass="inline", middle=False)

        self.span(title)

        self.close_a()

        self.close_div()


    #
    # Floating Options
    #

    def begin_floating_options(self, div_id, is_open):
        self.open_div(id_=div_id, class_=["view_form"], style="display: none" if not is_open else None)
        self.open_table(class_=["filterform"], cellpadding="0", cellspacing="0", border="0")
        self.open_tr()
        self.open_td()

    def end_floating_options(self, reset_url=None):
        self.close_td()
        self.close_tr()
        self.open_tr()
        self.open_td()
        self.button("apply", _("Apply"), "submit")
        if reset_url:
            self.buttonlink(reset_url, _("Reset to defaults"))

        self.close_td()
        self.close_tr()
        self.close_table()
        self.close_div()

    def render_floating_option(self, name, height, varprefix, valuespec, value):
        self.open_div(class_=["floatfilter", height, name])
        self.div(valuespec.title(), class_=["legend"])
        self.open_div(class_=["content"])
        valuespec.render_input(varprefix + name, value)
        self.close_div()
        self.close_div()


    #
    # HTML icon rendering
    #


    # FIXME: Change order of input arguments in one: icon and render_icon!!
    def icon(self, help, icon, **kwargs):

        #TODO: Refactor
        title = help
        icon_name = icon

        self.write_html(self.render_icon(icon_name=icon_name, help=title, **kwargs))


    def empty_icon(self):
        self.write_html(self.render_icon("images/trans.png"))


    def render_icon(self, icon_name, help=None, middle=True, id=None, cssclass=None, class_=None):

        # TODO: Refactor
        title    = help
        id_      = id

        attributes = {'title'   : title,
                      'id'      : id_,
                      'class'   : ["icon", cssclass],
                      'align'   : 'absmiddle' if middle else None,
                      'src'     : icon_name if "/" in icon_name else self._detect_icon_path(icon_name)}

        if class_:
            attributes['class'].extend(class_)

        return self._render_opening_tag('img', close_tag=True, **attributes)


    def _detect_icon_path(self, icon_name):
        # Detect whether or not the icon is available as images/icon_*.png
        # or images/icons/*.png. When an icon is available as internal icon,
        # always use this one
        is_internal = False
        rel_path = "share/check_mk/web/htdocs/images/icon_"+icon_name+".png"
        if os.path.exists(cmk.paths.omd_root+"/"+rel_path):
            is_internal = True
        elif os.path.exists(cmk.paths.omd_root+"/local/"+rel_path):
            is_internal = True

        if is_internal:
            return "images/icon_%s.png" % icon_name
        else:
            return "images/icons/%s.png" % icon_name


    def render_icon_button(self, url, help, icon, id=None, onclick=None,
                           style=None, target=None, cssclass=None, ty="button"):

        # TODO: Refactor
        title    = help
        id_      = id

        # TODO: Can we clean this up and move all button_*.png to internal_icons/*.png?
        if ty == "button":
            icon = "images/button_" + icon + ".png"

        icon = HTML(self.render_icon(icon, cssclass="iconbutton"))

        return self.render_a(icon, **{'title'   : title,
                                      'id'      : id_,
                                      'class'   : cssclass,
                                      'style'   : style,
                                      'target'  : target if target else '',
                                      'href'    : url if not onclick else "javascript:void(0)",
                                      'onfocus' : "if (this.blur) this.blur();",
                                      'onclick' : onclick })


    def icon_button(self, *args, **kwargs):
        self.write_html(self.render_icon_button(*args, **kwargs))


    def popup_trigger(self, *args, **kwargs):
        self.write_html(self.render_popup_trigger(*args, **kwargs))


    def render_popup_trigger(self, content, ident, what=None, data=None, url_vars=None,
                             style=None, menu_content=None, cssclass=None, onclose=None,
                             resizable=False):

        onclick = 'toggle_popup(event, this, %s, %s, %s, %s, %s, %s, %s);' % \
                    ("'%s'" % ident,
                     "'%s'" % what if what else 'null',
                     json.dumps(data) if data else 'null',
                     "'%s'" % self.urlencode_vars(url_vars) if url_vars else 'null',
                     "'%s'" % menu_content if menu_content else 'null',
                     "'%s'" % onclose.replace("'", "\\'") if onclose else 'null',
                     json.dumps(resizable))

        #TODO: Check if HTML'ing content is correct and necessary!
        atag = self.render_a(HTML(content), class_="popup_trigger",
                                            href="javascript:void(0);",
                                            onclick=onclick)

        return self.render_div(atag, class_=["popup_trigger", cssclass],
                                     id_="popup_trigger_%s" % ident,
                                     style = style)


    def element_dragger_url(self, dragging_tag, base_url):
        self.write_html(self.render_element_dragger(dragging_tag,
            drop_handler="function(index){return element_drag_url_drop_handler(%s, index);})" %
                                                                        json.dumps(base_url)))


    def element_dragger_js(self, dragging_tag, drop_handler, handler_args):
        self.write_html(self.render_element_dragger(dragging_tag,
            drop_handler="function(new_index){return %s(%s, new_index);})" %
                                                (drop_handler, json.dumps(handler_args))))


    # Currently only tested with tables. But with some small changes it may work with other
    # structures too.
    def render_element_dragger(self, dragging_tag, drop_handler):
        return self.render_a(self.render_icon("drag", _("Move this entry")),
            href="javascript:void(0)",
            class_=["element_dragger"],
            onmousedown="element_drag_start(event, this, %s, %s" %
                        (json.dumps(dragging_tag.upper()), drop_handler)
        )

    #
    # HTML - All the common and more complex HTML rendering methods
    #


    def _dump_get_vars(self):
        self.begin_foldable_container("html", "debug_vars", True, _("GET/POST variables of this page"))
        self.debug_vars(hide_with_mouse = False)
        self.end_foldable_container()


    def debug_vars(self, prefix=None, hide_with_mouse=True, vars=None):
        if not vars:
            vars = self.request.vars
        hover = "this.style.display=\'none\';"
        self.open_table(class_=["debug_vars"], onmouseover=hover if hide_with_mouse else None)
        self.tr(self.render_th(_("POST / GET Variables"), colspan="2"))
        for name, value in sorted(vars.items()):
            if name in [ "_password", "password" ]:
                value = "***"
            if not prefix or name.startswith(prefix):
                self.tr(self.render_td(name, class_="left") + self.render_td(value, class_="right"))
        self.close_table()



    # TODO: Rename the status_icons because they are not only showing states. There are also actions.
    # Something like footer icons or similar seems to be better
    def _write_status_icons(self):
        self.icon_button(self.makeuri([]), _("URL to this frame"),
                         "frameurl", target="_top", cssclass="inline")
        self.icon_button("index.py?" + self.urlencode_vars([("start_url", self.makeuri([]))]),
                         _("URL to this page including sidebar"),
                         "pageurl", target="_top", cssclass="inline")

        # TODO: Move this away from here. Make a context button. The view should handle this
        if self.myfile == "view" and self.var('mode') != 'availability':
            self.icon_button(self.makeuri([("output_format", "csv_export")]),
                             _("Export as CSV"),
                             "download_csv", target="_top", cssclass="inline")

        # TODO: This needs to be realized as plugin mechanism
        if self.myfile == "view":
            mode_name = self.var('mode') == "availability" and "availability" or "view"

            encoded_vars = {}
            for k, v in self.page_context.items():
                if v == None:
                    v = ''
                elif type(v) == unicode:
                    v = v.encode('utf-8')
                encoded_vars[k] = v

            self.popup_trigger(
                self.render_icon("menu", _("Add this view to..."), cssclass="iconbutton inline"),
                'add_visual', 'add_visual', data=[mode_name, encoded_vars, {'name': self.var('view_name')}],
                url_vars=[("add_type", mode_name)])

        # TODO: This should be handled by pagetypes.py
        elif self.myfile == "graph_collection":

            self.popup_trigger(
                self.render_icon("menu", _("Add this graph collection to..."),
                                 cssclass="iconbutton inline"),
                'add_visual', 'add_visual', data=["graph_collection", {}, {'name': self.var('name')}],
                url_vars=[("add_type", "graph_collection")])


        for img, tooltip in self.status_icons.items():
            if type(tooltip) == tuple:
                tooltip, url = tooltip
                self.icon_button(url, tooltip, img, cssclass="inline")
            else:
                self.icon(tooltip, img, cssclass="inline")

        if self.times:
            self.measure_time('body')
            self.open_div(class_=["execution_times"])
            entries = self.times.items()
            entries.sort()
            for name, duration in entries:
                self.div("%s: %.1fms" % (name, duration * 1000))
            self.close_div()


    #
    # Per request caching
    # TODO: Remove this cache here. Do we need some generic functionality
    # like this? Probably use some more generic / standard thing.
    #


    def set_cache(self, name, value):
        self.caches[name] = value
        return value


    def set_cache_default(self, name, value):
        if self.is_cached(name):
            return self.get_cached(name)
        else:
            return self.set_cache(name, value)


    def is_cached(self, name):
        return name in self.caches


    def get_cached(self, name):
        return self.caches.get(name)


    def del_cache(self, name):
        if name in self.caches:
            del self.caches[name]


    #
    # FIXME: Legacy functions
    #

    # TODO: Remove this specific legacy function. Change code using this to valuespecs
    def datetime_input(self, varname, default_value, submit=None):
        try:
            t = self.get_datetime_input(varname)
        except:
            t = default_value

        if varname in self.user_errors:
            self.add_user_error(varname + "_date", self.user_errors[varname])
            self.add_user_error(varname + "_time", self.user_errors[varname])
            self.set_focus(varname + "_date")

        br = time.localtime(t)
        self.date_input(varname + "_date", br.tm_year, br.tm_mon, br.tm_mday, submit=submit)
        self.write_text(" ")
        self.time_input(varname + "_time", br.tm_hour, br.tm_min, submit=submit)
        self.form_vars.append(varname + "_date")
        self.form_vars.append(varname + "_time")


    # TODO: Remove this specific legacy function. Change code using this to valuespecs
    def time_input(self, varname, hours, mins, submit=None):
        self.text_input(varname, "%02d:%02d" % (hours, mins), cssclass="time", size=5,
                        submit=submit, omit_css_width = True)


    # TODO: Remove this specific legacy function. Change code using this to valuespecs
    def date_input(self, varname, year, month, day, submit=None):
        self.text_input(varname, "%04d-%02d-%02d" % (year, month, day),
                        cssclass="date", size=10, submit=submit, omit_css_width = True)


    # TODO: Remove this specific legacy function. Change code using this to valuespecs
    def get_datetime_input(self, varname):
        t = self.var(varname + "_time")
        d = self.var(varname + "_date")
        if not t or not d:
            raise MKUserError([varname + "_date", varname + "_time"],
                              _("Please specify a date and time."))

        try:
            br = time.strptime(d + " " + t, "%Y-%m-%d %H:%M")
        except:
            raise MKUserError([varname + "_date", varname + "_time"],
                              _("Please enter the date/time in the format YYYY-MM-DD HH:MM."))
        return int(time.mktime(br))


    # TODO: Remove this specific legacy function. Change code using this to valuespecs
    def get_time_input(self, varname, what):
        t = self.var(varname)
        if not t:
            raise MKUserError(varname, _("Please specify %s.") % what)

        try:
            h, m = t.split(":")
            m = int(m)
            h = int(h)
            if m < 0 or m > 59 or h < 0:
                raise Exception()
        except:
            raise MKUserError(varname, _("Please enter the time in the format HH:MM."))
        return m * 60 + h * 3600
