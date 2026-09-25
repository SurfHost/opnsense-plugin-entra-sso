{#
 # Copyright (c) 2026 SurfHost.nl
 # SPDX-License-Identifier: MIT
 #}

<script>
    $( document ).ready(function() {
        var data_get_map = {'frm_general_settings':"/api/openvpnauthoauth2/settings/get"};
        mapDataToFormUI(data_get_map).done(function(data){
            formatTokenizersUI();
            $('.selectpicker').selectpicker('refresh');
        });

        updateServiceControlUI('openvpnauthoauth2');

        var label_classes = {
            'good': 'label-success',
            'warn': 'label-warning',
            'bad': 'label-danger',
            'off': 'label-default'
        };

        // text is a translation, which lang._() has already made HTML safe
        function labelHtml(cls, text) {
            return '<span class="label ' + label_classes[cls] + '">' + text + '</span>';
        }

        function statusLabel(state, text_map) {
            return labelHtml(state, text_map[state]);
        }

        // lang._() HTML-escapes its result (quotes and '>' become entities),
        // so decode a translation before it goes through .text(); a textarea
        // parses no markup, only character references
        var decoder = document.createElement('textarea');
        function plainText(html) {
            decoder.innerHTML = html;
            return decoder.value;
        }

        // fill the %s placeholders of a translation in order; a replacer
        // function keeps '$' sequences in server-provided values literal
        function fillIn(html, values) {
            var i = 0;
            return plainText(html).replace(/%s/g, function () {
                return String(values[i++]);
            });
        }

        function updateStatus() {
            ajaxCall("/api/openvpnauthoauth2/service/details", {}, function(data, status) {
                if (status !== "success" || data['result'] !== 'ok') {
                    $("#status_rows").hide();
                    $("#status_error").show();
                    return;
                }
                $("#status_error").hide();
                $("#status_rows").show();
                var running_map = {
                    'good': "{{ lang._('running') }}",
                    'warn': "{{ lang._('running, not consulted') }}",
                    'bad': "{{ lang._('not running') }}",
                    'off': "{{ lang._('disabled') }}"
                };
                var enabled = data['enabled'] === true;
                var enforcement = data['enforcement'] || {};
                var enforcement_state = enabled ? enforcement['state'] : 'disabled';
                var auto_fix = data['auto_fix'] !== false;
                var saved = data['saved_directives'];
                $("#status_supervisor").html(
                    statusLabel(!enabled ? 'off' : (data['supervisor'] ? 'good' : 'bad'), running_map));
                // OpenVPN only asks the daemon while it runs with management-client-auth
                var consulted = enforcement_state === 'enforced' || enforcement_state === 'verifying';
                $("#status_daemon").html(statusLabel(
                    !enabled ? 'off' : (!data['daemon'] ? 'bad' : (consulted ? 'good' : 'warn')), running_map));
                $("#status_swap").html(statusLabel(
                    data['swap'] === 'active' ? 'good' : (data['swap'] === 'disabled' ? 'off' : 'bad'), {
                        'good': "{{ lang._('active') }}",
                        'bad': "{{ lang._('inactive') }}",
                        'off': "{{ lang._('n/a') }}"
                    }));
                $("#status_listener").html(
                    statusLabel(!enabled ? 'off' : (data['listener'] ? 'good' : 'bad'), {
                        'good': "{{ lang._('listening') }}",
                        'bad': "{{ lang._('not listening') }}",
                        'off': "{{ lang._('disabled') }}"
                    }));
                $("#status_listen_addr").text(enabled && data['listen'] ? data['listen'] : '');

                var binds = data['listen_binds'] || [];
                var conflict = data['listen_conflict'] || [];
                if (!enabled) {
                    $("#status_listen_binds").text('');
                } else if (binds.length === 0) {
                    $("#status_listen_binds").text("{{ lang._('nothing is listening on this port') }}");
                } else {
                    $("#status_listen_binds").text(
                        "{{ lang._('bound to') }} " + binds.join(', '));
                }
                if (enabled && conflict.length > 0) {
                    $("#port_conflict_warning")
                        .text("{{ lang._('This port is also held by another process:') }} " +
                              conflict.join(', ') + ". " +
                              "{{ lang._('Pick a free port; the web GUI PHP backend already uses 9000.') }}")
                        .show();
                } else {
                    $("#port_conflict_warning").hide();
                }

                var burl = data['base_url'] || {};
                var problems = burl['problems'] || [];
                $("#status_baseurl").html(statusLabel(
                    !enabled ? 'off' : (problems.length === 0 ? 'good' : 'bad'), {
                        'good': "{{ lang._('consistent') }}",
                        'bad': "{{ lang._('check settings') }}",
                        'off': "{{ lang._('disabled') }}"
                    }));
                if (enabled && problems.length > 0) {
                    var list = $("<ul/>");
                    $.each(problems, function (i, problem) {
                        list.append($("<li/>").text(problem));
                    });
                    $("#baseurl_warning").empty()
                        .append($("<b/>").text("{{ lang._('Callback URL problems') }}"))
                        .append(list)
                        .show();
                } else {
                    $("#baseurl_warning").hide();
                }

                // the security row: what the running OpenVPN process loaded,
                // which stays fixed for the life of that process
                var enforcement_labels = {
                    'enforced': ['good', "{{ lang._('enforced') }}"],
                    'verifying': ['off', "{{ lang._('verifying') }}"],
                    'not_running': ['off', "{{ lang._('instance not running') }}"],
                    'repairing': ['warn', "{{ lang._('restarting with the SSO directives') }}"],
                    'stopping': ['bad', "{{ lang._('NOT ENFORCED, stopping instance') }}"],
                    'open': ['bad', "{{ lang._('NOT ENFORCED') }}"],
                    'unverified': ['bad', "{{ lang._('cannot verify') }}"],
                    'held_down': ['bad', "{{ lang._('instance kept stopped') }}"],
                    'not_watched': ['bad', "{{ lang._('not watched') }}"],
                    'no_instance': ['bad', "{{ lang._('instance not found') }}"],
                    'disabled': ['off', "{{ lang._('disabled') }}"]
                };
                var enforcement_label = enforcement_labels.hasOwnProperty(enforcement_state) ?
                    enforcement_labels[enforcement_state] : ['off', "{{ lang._('unknown') }}"];
                $("#status_enforcement").html(labelHtml(enforcement_label[0], enforcement_label[1]));

                var details = [];
                if (enabled) {
                    if (enforcement['pid']) {
                        details.push(['', fillIn("{{ lang._('OpenVPN pid %s') }}", [enforcement['pid']])]);
                    }
                    if (enforcement['reason']) {
                        details.push(['', enforcement['reason']]);
                    }
                    if (enforcement['stops'] > 0) {
                        details.push(['', fillIn(
                            "{{ lang._('stopped %s times, restarted %s times since the service started') }}",
                            [enforcement['stops'], enforcement['repairs'] || 0])]);
                    }
                    if (enforcement_state === 'enforced' && enforcement['optional_loaded'] === false) {
                        details.push(['text-warning', plainText(
                            "{{ lang._('certificate-only logins are refused: auth-user-pass-optional missing') }}")]);
                    }
                    if (data['pre_apply_hook'] === false) {
                        details.push(['text-warning', plainText(
                            "{{ lang._('pre-Apply hook missing: core changed, see the log') }}")]);
                    }
                }
                var detail = $("#status_enforcement_detail").empty();
                $.each(details, function (i, line) {
                    if (i > 0) {
                        detail.append($("<br/>"));
                    }
                    detail.append($("<span/>").addClass(line[0]).text(line[1]));
                });

                var reason = enforcement['reason'] || plainText("{{ lang._('unknown') }}");
                var alert_text = '';
                if (enforcement_state === 'open' || enforcement_state === 'stopping') {
                    alert_text = plainText("{{ lang._("The OpenVPN instance is running without 'management-client-auth', so anyone holding a valid client certificate for it connects without signing in. While the SSO service runs it stops such an instance within a second. If this message stays, stop the instance on System > Diagnostics > Services and check the SSO log.") }}");
                } else if (enforcement_state === 'unverified') {
                    alert_text = fillIn("{{ lang._("The SSO guard cannot prove that the running OpenVPN instance loaded 'management-client-auth' (%s). It restarts the instance to be sure. If this message stays, check the SSO log.") }}", [reason]);
                } else if (enforcement_state === 'held_down') {
                    alert_text = fillIn("{{ lang._("The OpenVPN instance started without 'management-client-auth'. The SSO guard stopped it, which dropped every session it had, and keeps it stopped: %s.") }}", [reason]) + ' ' + (auto_fix ?
                        plainText("{{ lang._('Press Apply on VPN > OpenVPN > Instances to restore the directives and start it.') }}") :
                        plainText("{{ lang._("Turn on 'Repair OpenVPN instance directives' under Advanced and press Save, or add the directives yourself and press Apply on VPN > OpenVPN > Instances.") }}"));
                } else if (enforcement_state === 'not_watched' && data['supervisor'] === true) {
                    // the service runs but its guard has not reported recently
                    alert_text = plainText("{{ lang._("The SSO guard is not reporting, so nothing stops the OpenVPN instance if it ever starts without 'management-client-auth'. Apply, boot and CARP still restore the directives first, but a restart from the dashboard or System > Diagnostics > Services is not checked. If this message stays, restart the service above and check the SSO log.") }}");
                } else if (enforcement_state === 'not_watched') {
                    alert_text = plainText("{{ lang._("The SSO service is not running, so nothing stops the OpenVPN instance if it ever starts without 'management-client-auth'. Apply, boot and CARP still restore the directives first, but a restart from the dashboard or System > Diagnostics > Services is not checked. Start the service above.") }}");
                } else if (enforcement_state === 'no_instance') {
                    alert_text = plainText("{{ lang._('The OpenVPN instance selected on this page no longer exists, so no instance is protected. Select the instance to protect and press Save.') }}");
                }
                if (alert_text !== '') {
                    $("#enforcement_alert").text(alert_text).show();
                } else {
                    $("#enforcement_alert").hide();
                }

                // config.xml, which core rewrites without the directives
                // whenever the instance itself is saved
                var saved_ok = !!(saved && saved['client_auth'] && saved['optional']);
                var saved_label;
                if (!enabled) {
                    saved_label = labelHtml('off', "{{ lang._('disabled') }}");
                } else if (!saved) {
                    saved_label = labelHtml('off', "{{ lang._('unknown') }}");
                } else if (saved['instance'] !== true) {
                    saved_label = labelHtml('bad', "{{ lang._('instance not found') }}");
                } else if (saved_ok) {
                    saved_label = labelHtml('good', "{{ lang._('present') }}");
                } else if (auto_fix) {
                    saved_label = labelHtml('warn', "{{ lang._('missing, restoring') }}");
                } else {
                    saved_label = labelHtml('bad', "{{ lang._('missing: the next start will be stopped') }}");
                }
                $("#status_client_auth").html(saved_label);
                if (enabled && saved && saved['instance'] === true && !saved_ok && enforcement_state === 'enforced') {
                    $("#client_auth_warning").text(
                        plainText("{{ lang._('The directives are missing from the saved configuration, usually because the instance was saved in VPN > OpenVPN > Instances, whose Options field cannot show them. The running instance still enforces SSO.') }}") + ' ' + (auto_fix ?
                        plainText("{{ lang._('They are restored automatically within a few seconds, and before every Apply.') }}") :
                        plainText("{{ lang._("'Repair OpenVPN instance directives' is off, so the next start of this instance will be stopped.") }}"))
                    ).show();
                } else {
                    $("#client_auth_warning").hide();
                }

                var token = (enabled && saved && saved['instance'] === true) ? saved['token'] : null;
                var token_labels = {
                    'injected': ['good', "{{ lang._('on') }}"],
                    'instance': ['warn', "{{ lang._("instance's Auth Token Lifetime in use") }}"],
                    'missing': ['warn', "{{ lang._('off') }}"]
                };
                var token_label = !enabled ? ['off', "{{ lang._('disabled') }}"] :
                    (token_labels.hasOwnProperty(token) ? token_labels[token] : ['off', "{{ lang._('unknown') }}"]);
                $("#status_token").html(labelHtml(token_label[0], token_label[1]));
                if (token === 'instance' || token === 'missing') {
                    $("#token_info").show();
                } else {
                    $("#token_info").hide();
                }
            });
        }

        updateStatus();
        setInterval(updateStatus, 10000);

        // move the "generate secret" button next to the Encryption secret
        // label, the way core does for the instance auth-token secret
        $("#control_label_openvpnauthoauth2\\.http\\.secret").before($("#gensecret_div").detach().show());

        $("#gensecret").click(function(){
            ajaxCall("/api/openvpnauthoauth2/settings/gen_secret", {}, function(data, status) {
                var help = $("#help_block_openvpnauthoauth2\\.http\\.secret");
                if (status !== "success" || data['result'] !== 'ok') {
                    help.text("{{ lang._('Could not generate a secret on the firewall.') }}");
                    return;
                }
                // a password field masks the new value, so confirm it in words;
                // clear a validation error left by an earlier failed Save
                $("*[id$='openvpnauthoauth2.http.secret']").removeClass("has-error");
                $("#openvpnauthoauth2\\.http\\.secret").val(data['secret']).change();
                help.text("{{ lang._('New secret generated. Click Save to store it.') }}");
            });
        });

        // the logo travels as a data URI in the hidden field the form has
        // for it, so Save stores it with the rest. The picker follows core's
        // 'file' field type, which itself stores bare base64 without the
        // image type the page needs.
        var logo_types = ['image/png', 'image/jpeg', 'image/svg+xml', 'image/webp'];
        var logo_max_bytes = 65536; // LOGO_MAX_BYTES in the model
        var logo_field = $("#openvpnauthoauth2\\.page\\.logo");
        var logo_help = $("#help_block_openvpnauthoauth2\\.page\\.logo");
        // row, label, field and help block, which a failed Save marks has-error;
        // a refused file is marked the same way, a picked or removed logo clears it
        var logo_row = $("*[id$='openvpnauthoauth2.page.logo']");
        logo_field.after($("#logo_div").detach().show());

        // setFormData fires change once it has filled the field
        logo_field.change(function () {
            var uri = $(this).val();
            if (uri !== '') {
                $("#logo_preview").attr('src', uri).show();
            } else {
                $("#logo_preview").removeAttr('src').hide();
            }
            $("#logo_remove").toggle(uri !== '');
        });

        $("#logo_div input[type=file]").change(function () {
            var file = this.files[0];
            // lets the same file be picked again, e.g. after Remove
            this.value = '';
            if (!file) {
                return;
            }
            if (logo_types.indexOf(file.type) === -1) {
                logo_row.addClass("has-error");
                logo_help.text("{{ lang._('This file is not a PNG, JPEG, SVG or WebP image.') }}");
                return;
            }
            // an empty file would become a data URI without data, which the model refuses
            if (file.size === 0 || file.size > logo_max_bytes) {
                logo_row.addClass("has-error");
                logo_help.text("{{ lang._('This image is empty or larger than 64 KB. Choose another file.') }}");
                return;
            }
            var reader = new FileReader();
            reader.onload = function (event) {
                logo_row.removeClass("has-error");
                logo_field.val(event.target.result).change();
                logo_help.text("{{ lang._('Click Save to store the logo.') }}");
            };
            reader.readAsDataURL(file);
        });

        $("#logo_remove").click(function () {
            logo_row.removeClass("has-error");
            logo_field.val('').change();
            logo_help.text("{{ lang._('Click Save to remove the logo.') }}");
        });

        $("#saveAct").click(function(){
            saveFormToEndpoint("/api/openvpnauthoauth2/settings/set", 'frm_general_settings', function(){
                ajaxCall("/api/openvpnauthoauth2/service/reconfigure", {}, function(data,status) {
                    updateServiceControlUI('openvpnauthoauth2');
                    updateStatus();
                });
            });
        });
    });
</script>

<div class="content-box" style="padding-bottom: 1.5em;">
    <div class="col-md-12">
        <h2>{{ lang._('Status') }}</h2>
        <div id="status_error" style="display:none;">
            <span class="label label-default">{{ lang._('status unavailable') }}</span>
        </div>
        <table id="status_rows" class="table table-condensed" style="max-width: 40em;">
            <tbody>
                <tr>
                    <td>{{ lang._('Supervisor') }}</td>
                    <td id="status_supervisor"></td>
                </tr>
                <tr>
                    <td>{{ lang._('SSO daemon') }}</td>
                    <td id="status_daemon"></td>
                </tr>
                <tr>
                    <td>{{ lang._('Management socket swap') }}</td>
                    <td id="status_swap"></td>
                </tr>
                <tr>
                    <td>
                        {{ lang._('Callback listener') }}
                        <br/><small class="text-muted" id="status_listen_addr"></small>
                        <br/><small class="text-muted" id="status_listen_binds"></small>
                    </td>
                    <td id="status_listener"></td>
                </tr>
                <tr>
                    <td>{{ lang._('Public base URL') }}</td>
                    <td id="status_baseurl"></td>
                </tr>
                <tr>
                    <td>
                        {{ lang._('SSO enforcement (running instance)') }}
                        <br/><small class="text-muted" id="status_enforcement_detail" style="overflow-wrap: anywhere;"></small>
                    </td>
                    <td id="status_enforcement"></td>
                </tr>
                <tr>
                    <td>{{ lang._('Saved instance directives') }}</td>
                    <td id="status_client_auth"></td>
                </tr>
                <tr>
                    <td>{{ lang._('Silent token renewal') }}</td>
                    <td id="status_token"></td>
                </tr>
            </tbody>
        </table>
        <div id="enforcement_alert" class="alert alert-danger" style="display:none; max-width: 60em;"></div>
        <div id="baseurl_warning" class="alert alert-warning" style="display:none; max-width: 60em;"></div>
        <div id="port_conflict_warning" class="alert alert-danger" style="display:none; max-width: 60em;"></div>
        <div id="client_auth_warning" class="alert alert-warning" style="display:none; max-width: 60em;"></div>
        <div id="token_info" class="alert alert-info" style="display:none; max-width: 60em;">
            {{ lang._("Silent token renewal needs the 'auth-gen-token ... external-auth' directive on the selected instance. Without it, OpenVPN judges auth tokens itself, rejects them at the first renegotiation, and clients fall back to a browser login about once an hour. The plugin adds the directive only while 'Repair OpenVPN instance directives' under Advanced is on and the instance's own 'Auth Token Lifetime' field is empty: a value there emits a second 'auth-gen-token' line, so the plugin leaves its own out to keep the instance bootable. SSO enforcement does not depend on it.") }}
        </div>
    </div>
</div>

<div class="content-box" style="padding-bottom: 1.5em;">
    <span id="gensecret_div" style="display:none" class="pull-right">
        <button id="gensecret" type="button" class="btn btn-secondary" title="{{ lang._('Generate a new secret on the firewall.') }}" data-toggle="tooltip">
            <i class="fa fa-fw fa-gear"></i>
        </button>
    </span>
    <div id="logo_div" style="display:none">
        <img id="logo_preview" alt="" style="display:none; box-sizing: content-box; max-width: 200px; max-height: 48px; margin-bottom: 6px; padding: 4px; background: #fff; border: 1px solid #ddd; border-radius: 4px;">
        <div>
            <label class="btn btn-default" style="margin-bottom: 0;">
                <i class="fa fa-fw fa-folder-o"></i> {{ lang._('Choose image') }}
                <input type="file" accept="image/png,image/jpeg,image/svg+xml,image/webp" style="display: none;">
            </label>
            <button id="logo_remove" type="button" class="btn btn-default" style="display:none">
                <i class="fa fa-fw fa-trash-o"></i> {{ lang._('Remove') }}
            </button>
        </div>
    </div>
    {{ partial("layout_partials/base_form",['fields':generalForm,'id':'frm_general_settings']) }}
    <div class="col-md-12">
        <hr/>
        <button class="btn btn-primary" id="saveAct" type="button"><b>{{ lang._('Save') }}</b> <i id="saveAct_progress"></i></button>
    </div>
</div>
