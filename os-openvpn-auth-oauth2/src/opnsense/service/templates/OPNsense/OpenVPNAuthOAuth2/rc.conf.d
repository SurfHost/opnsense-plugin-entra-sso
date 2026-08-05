{# boot autostart flag for /usr/local/etc/rc.d/openvpnauthoauth2 #}
{% if helpers.exists('OPNsense.OpenVPNAuthOAuth2.general.enabled') and OPNsense.OpenVPNAuthOAuth2.general.enabled == '1' %}
openvpnauthoauth2_enable="YES"
{% else %}
openvpnauthoauth2_enable="NO"
{% endif %}
