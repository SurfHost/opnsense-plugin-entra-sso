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

        function statusLabel(state, text_map) {
            var cls = {'good': 'label-success', 'bad': 'label-danger', 'off': 'label-default'}[state];
            return '<span class="label ' + cls + '">' + text_map[state] + '</span>';
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
                    'bad': "{{ lang._('not running') }}",
                    'off': "{{ lang._('disabled') }}"
                };
                var enabled = data['enabled'] === true;
                $("#status_supervisor").html(
                    statusLabel(!enabled ? 'off' : (data['supervisor'] ? 'good' : 'bad'), running_map));
                $("#status_daemon").html(
                    statusLabel(!enabled ? 'off' : (data['daemon'] ? 'good' : 'bad'), running_map));
                $("#status_swap").html(statusLabel(
                    data['swap'] === 'active' ? 'good' : (data['swap'] === 'disabled' ? 'off' : 'bad'), {
                        'good': "{{ lang._('active') }}",
                        'bad': "{{ lang._('inactive') }}",
                        'off': "{{ lang._('n/a') }}"
                    }));
                $("#status_listener").html(
                    statusLabel(!enabled ? 'off' : (data['listener'] ? 'good' : 'bad'), {
                        'good': "{{ lang._('reachable') }}",
                        'bad': "{{ lang._('unreachable') }}",
                        'off': "{{ lang._('disabled') }}"
                    }));
                var flag = data['client_auth_flag'];
                $("#status_client_auth").html(
                    statusLabel(flag === true ? 'good' : (flag === false ? 'bad' : 'off'), {
                        'good': "{{ lang._('present') }}",
                        'bad': "{{ lang._('missing') }}",
                        'off': "{{ lang._('unknown') }}"
                    }));
                if (flag === false) {
                    $("#client_auth_warning").show();
                } else {
                    $("#client_auth_warning").hide();
                }
            });
        }

        updateStatus();
        setInterval(updateStatus, 10000);

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
                    <td>{{ lang._('Callback listener') }}</td>
                    <td id="status_listener"></td>
                </tr>
                <tr>
                    <td>{{ lang._("OpenVPN 'management-client-auth'") }}</td>
                    <td id="status_client_auth"></td>
                </tr>
            </tbody>
        </table>
        <div id="client_auth_warning" class="alert alert-warning" style="display:none; max-width: 60em;">
            {{ lang._("The selected OpenVPN instance does not carry the 'management-client-auth' directive, so OpenVPN never asks this service to authorize client connects and SSO stays silent. The instance's Options field does not offer this directive and drops it whenever that instance is saved. Press Save below to add it back and restart the instance (this drops its active tunnels), or disable 'Repair OpenVPN instance flag' under Advanced to manage the directive yourself.") }}
        </div>
    </div>
</div>

<div class="content-box" style="padding-bottom: 1.5em;">
    {{ partial("layout_partials/base_form",['fields':generalForm,'id':'frm_general_settings']) }}
    <div class="col-md-12">
        <hr/>
        <button class="btn btn-primary" id="saveAct" type="button"><b>{{ lang._('Save') }}</b> <i id="saveAct_progress"></i></button>
    </div>
</div>
