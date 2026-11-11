pipeline {

    agent {
        label 'linux-python'
    }

    triggers {
        cron('TZ=Europe/Paris\nH 7 * * *')
    }

    parameters {

        string(
            name: 'REPORT_DATE',
            defaultValue: '',
            description: 'YYYY-MM-DD, empty = yesterday'
        )

        booleanParam(
            name: 'SEND_MAIL',
            defaultValue: true,
            description: 'Send report by email'
        )
    }

    environment {

        CONFIG_FILE = 'config.yaml'

        OUTPUT_DIR = 'out'

        LOG_DIR = 'logs'

        FAILURE_EMAIL = 'backup-team@groupeseb.com'

        VENV_DIR = '.venv'

        PIP_CACHE_DIR = "${WORKSPACE}/.pip-cache"
    }

    options {

        timeout(
            time: 30,
            unit: 'MINUTES'
        )

        disableConcurrentBuilds()

        timestamps()

        buildDiscarder(
            logRotator(
                daysToKeepStr: '90',
                numToKeepStr: '90',
                artifactDaysToKeepStr: '30',
                artifactNumToKeepStr: '30'
            )
        )
    }

    stages {

        stage('Initialize Build') {

            steps {

                script {

                    currentBuild.displayName =
                        "#${env.BUILD_NUMBER}-${params.REPORT_DATE ?: 'YESTERDAY'}"

                }

                cleanWs()

                sh '''
                    set -eu

                    mkdir -p "$OUTPUT_DIR"
                    mkdir -p "$LOG_DIR"

                    rm -rf "$OUTPUT_DIR"/*
                    rm -rf "$LOG_DIR"/*
                '''
            }
        }

        stage('Validate Parameters') {

            steps {

                sh '''
                    set -eu

                    if [ -n "$REPORT_DATE" ]; then

                        echo "$REPORT_DATE" | \
                        grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' >/dev/null || {
                            echo "REPORT_DATE must be YYYY-MM-DD"
                            exit 1
                        }

                        date -d "$REPORT_DATE" >/dev/null 2>&1 || {
                            echo "Invalid calendar date"
                            exit 1
                        }
                    fi

                    echo "Parameter validation successful"
                '''
            }
        }

        stage('Setup Python Environment') {

            steps {

                retry(3) {

                    sh '''
                        set -eu

                        rm -rf "$VENV_DIR"

                        python3 -m venv "$VENV_DIR"

                        $VENV_DIR/bin/python \
                            -m pip install --upgrade pip

                        $VENV_DIR/bin/pip install \
                            --cache-dir "$PIP_CACHE_DIR" \
                            -r requirements.txt

                        $VENV_DIR/bin/pip install \
                            --cache-dir "$PIP_CACHE_DIR" \
                            pytest \
                            pytest-cov

                        $VENV_DIR/bin/python --version

                        echo "Python environment ready"
                    '''
                }
            }
        }

        stage('Preflight Validation') {

            steps {

                sh '''
                    set -eu

                    [ -f "$CONFIG_FILE" ] || {
                        echo "Missing config file: $CONFIG_FILE"
                        exit 1
                    }

                    echo "Preflight validation successful"
                '''
            }
        }

        stage('Tests & Coverage') {

            steps {

                sh '''
                    set -eu

                    echo "==========================================="
                    echo "Executing Tests With Coverage"
                    echo "==========================================="

                    $VENV_DIR/bin/python -m pytest \
                        tests \
                        -v \
                        --junitxml=test-results.xml

                    echo "All tests passed"
                '''
            }

            post {

                always {

                    junit(
                        testResults: 'test-results.xml',
                        allowEmptyResults: false
                    )
                }
            }
        }

        stage('Generate Veeam Report') {

            steps {

                withCredentials([
                    usernamePassword(
                        credentialsId: 'veeam-em-report',
                        usernameVariable: 'VEEAM_EM_USER',
                        passwordVariable: 'VEEAM_EM_PASSWORD'
                    )
                ]) {

                    timeout(
                        time: 15,
                        unit: 'MINUTES'
                    ) {

                        sh '''
                            set +x
                            set -eu

                            test -n "$VEEAM_EM_USER" || {
                                echo "VEEAM_EM_USER missing"
                                exit 1
                            }

                            test -n "$VEEAM_EM_PASSWORD" || {
                                echo "VEEAM_EM_PASSWORD missing"
                                exit 1
                            }

                            mkdir -p "$OUTPUT_DIR"
                            mkdir -p "$LOG_DIR"

                            ARGS="--config $CONFIG_FILE --out $OUTPUT_DIR"

                            if [ -n "$REPORT_DATE" ]; then
                                ARGS="$ARGS --date $REPORT_DATE"
                            fi

                            if [ "$SEND_MAIL" = "true" ]; then
                                ARGS="$ARGS --send"
                                echo "Generating report and sending email"
                            else
                                echo "Generating report only"
                            fi

                            echo "Running VDR"

                            $VENV_DIR/bin/python -m vdr $ARGS \
                                > "$LOG_DIR/vdr.log" 2>&1

                            test -s "$OUTPUT_DIR/report.html" || {
                                echo "report.html missing or empty"
                                exit 1
                            }

                            test -s "$OUTPUT_DIR/report.json" || {
                                echo "report.json missing or empty"
                                exit 1
                            }

                            grep -qi "<html" \
                                "$OUTPUT_DIR/report.html" || {
                                echo "Invalid HTML report"
                                exit 1
                            }

                            $VENV_DIR/bin/python - <<'PY'
import json

with open("out/report.json", encoding="utf-8") as f:
    data = json.load(f)

assert isinstance(data, dict), "JSON root must be dict"
assert "summary" in data, "Missing summary section"

print("JSON validation passed")
PY

                            echo "Report validation successful"
                        '''
                    }
                }
            }
        }
    }

    post {

        success {

            echo 'Veeam report completed successfully.'
        }

        failure {

            mail(
                to: "${FAILURE_EMAIL}",
                subject: "Veeam Daily Report FAILED - Build #${env.BUILD_NUMBER}",
                body: """
Jenkins Job Failed

Job Name: ${env.JOB_NAME}
Build Number: ${env.BUILD_NUMBER}
Build URL: ${env.BUILD_URL}

Please review console logs, test results and archived artifacts.
"""
            )
        }

        always {

            archiveArtifacts(
                artifacts: '''
                    out/**/*,
                    logs/**/*,
                    coverage.xml,
                    test-results.xml
                ''',
                fingerprint: true,
                allowEmptyArchive: true
            )

            cleanWs(
                deleteDirs: true,
                disableDeferredWipeout: true
            )
        }
    }
}